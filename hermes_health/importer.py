"""Import an Apple Health export (``export.xml``) — the backfill.

A full export is 150 MB+ and hundreds of thousands of records, so it is
streamed with ``iterparse`` and never loaded whole. Quantity records become
samples, sleep-analysis records become sleep spans, workouts become workouts,
and the activity rings' goals come from ``ActivitySummary`` instead of being
guessed.

The import is idempotent: re-running it changes nothing.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from xml.etree.ElementTree import Element

from . import config
from .metrics import Metric, activity_slug, compact, is_sleep, resolve, sleep_stage, slug
from .payloads import parse_ts
from .store import SleepSpan, Sample, Workout, insert_raw, set_meta, write_sleep, write_workouts

BATCH_SIZE = 25000

_SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
_STAND_HOUR_TYPE = "HKCategoryTypeIdentifierAppleStandHour"
_STOOD_VALUES = ("HKCategoryValueAppleStandHourStood", "stood")

# WorkoutStatistics ``type`` -> the field it fills on a Workout. Keyed by the
# stripped slug, so the HealthKit identifier and a plain name both match.
_STAT_TYPES = {
    "activeenergyburned": ("energy_kcal", 1.0),
    "totalenergyburned": ("energy_kcal", 1.0),
    "basalenergyburned": ("basal_energy_kcal", 1.0),
    "distancewalkingrunning": ("distance_m", 1609.344),
    "distancecycling": ("distance_m", 1609.344),
    "distanceswimming": ("distance_m", 0.9144),
    "distanceswimmingpool": ("distance_m", 0.9144),
    "distancewheelchair": ("distance_m", 1609.344),
    "heartrate": ("heart_rate", 1.0),
    "runningpower": ("running_power_w", 1.0),
    "cyclingpower": ("cycling_power_w", 1.0),
    "strokecount": ("strokes", 1.0),
    "swimmingstrokecount": ("strokes", 1.0),
    "swimmingstrokecounts": ("strokes", 1.0),
}


@dataclass
class ImportReport:
    path: str = ""
    records_read: int = 0
    samples_inserted: int = 0
    samples_duplicate: int = 0
    samples_skipped: int = 0
    sleep_spans: int = 0
    workouts: int = 0
    activity_days: int = 0
    goals: Dict[str, float] = field(default_factory=dict)
    types: Counter = field(default_factory=Counter)
    categories_ignored: Counter = field(default_factory=Counter)
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    #: What the phone said about itself, so nobody has to guess later.
    profile: Dict[str, str] = field(default_factory=dict)
    export_date: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out = dict(self.__dict__)
        out["types"] = dict(self.types.most_common())
        out["categories_ignored"] = dict(self.categories_ignored.most_common())
        out["first_ts_iso"] = _iso(self.first_ts)
        out["last_ts_iso"] = _iso(self.last_ts)
        out["records_read"] = self.records_read
        out["unique_types"] = len(self.types)
        return out

    def lines(self) -> List[str]:
        return [
            f"records read:      {self.records_read:,}",
            f"samples stored:    {self.samples_inserted:,} (new) / {self.samples_duplicate:,} already present",
            f"samples skipped:   {self.samples_skipped:,}",
            f"sleep spans:       {self.sleep_spans:,}",
            f"workouts:          {self.workouts:,}",
            f"unique types:      {len(self.types)}",
            f"data range:        {_iso(self.first_ts)} -> {_iso(self.last_ts)}",
        ]


def _iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), config.tzinfo()).strftime("%Y-%m-%d %H:%M")


def import_export(
    conn: sqlite3.Connection,
    path: str,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    types: Optional[Sequence[str]] = None,
    batch_size: int = BATCH_SIZE,
    progress: Optional[Callable[[str], None]] = None,
) -> ImportReport:
    """Stream ``export.xml`` into the store. Returns an :class:`ImportReport`."""
    export_path = Path(path).expanduser()
    if not export_path.exists():
        raise FileNotFoundError(str(export_path))

    since_ts = parse_ts(since)[0] if since else None
    until_ts = parse_ts(until)[0] if until else None
    wanted: Optional[Set[str]] = {slug(t) for t in types} if types else None

    report = ImportReport(path=str(export_path))
    buffer: List[Sample] = []
    sleep_buffer: List[SleepSpan] = []
    workout_rows: List[Workout] = []
    goals: Dict[str, float] = {}
    export_src = "export"

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        result = insert_raw(conn, buffer, source=export_src)
        report.samples_inserted += result.inserted
        report.samples_duplicate += result.duplicates
        report.samples_skipped += result.skipped
        buffer = []

    for _event, elem in ET.iterparse(str(export_path), events=("end",)):
        tag = elem.tag
        if tag == "Record":
            report.records_read += 1
            if report.records_read % 100000 == 0 and progress:
                progress(f"  ... {report.records_read:,} records")
            record_type = elem.get("type") or ""
            type_key = slug(record_type)
            stamp = parse_ts(elem.get("startDate"))
            end_stamp = parse_ts(elem.get("endDate"))
            if stamp is None:
                report.samples_skipped += 1
                elem.clear()
                continue
            ts, offset = stamp
            if since_ts and ts < since_ts:
                elem.clear()
                continue
            if until_ts and ts > until_ts:
                elem.clear()
                continue
            if wanted is not None and type_key not in wanted:
                elem.clear()
                continue
            source = (elem.get("sourceName") or export_src).strip() or export_src
            unit = (elem.get("unit") or "").strip()
            value_text = elem.get("value") or ""

            if record_type == _SLEEP_TYPE:
                stage = sleep_stage(value_text) or "asleep"
                if end_stamp is not None:
                    sleep_buffer.append(SleepSpan(ts, end_stamp[0], stage, source))
                    report.sleep_spans += 1
                else:
                    report.samples_skipped += 1
                report.types[type_key] += 1
                elem.clear()
                continue

            if record_type == _STAND_HOUR_TYPE:
                if any(token in value_text for token in _STOOD_VALUES):
                    buffer.append(Sample("stand_hours", ts, 1.0, unit="count", source=source, offset_s=None))
                    report.types[type_key] += 1
                else:
                    report.categories_ignored[type_key] += 1
                elem.clear()
                continue

            if record_type.startswith("HKCategoryTypeIdentifier"):
                report.categories_ignored[type_key] += 1
                elem.clear()
                continue

            number = _float(value_text)
            if number is None:
                report.samples_skipped += 1
                elem.clear()
                continue
            buffer.append(Sample(record_type, ts, number, unit=unit, source=source, offset_s=offset))
            report.types[type_key] += 1
            if report.first_ts is None or ts < report.first_ts:
                report.first_ts = ts
            if report.last_ts is None or ts > report.last_ts:
                report.last_ts = ts
            if len(buffer) >= batch_size:
                flush()
                if progress:
                    progress(f"  ... {report.records_read:,} records, {report.samples_inserted:,} stored")
            elem.clear()
            continue

        if tag == "Workout":
            report.records_read += 1
            workout = _workout_from_element(elem, export_src)
            if workout is not None and _in_window(workout.start_ts, since_ts, until_ts):
                workout_rows.append(workout)
            elem.clear()
            continue

        if tag == "ActivitySummary":
            date_text = elem.get("dateComponents") or ""
            if date_text:
                report.activity_days += 1
                for attr, goal_key in (
                    ("activeEnergyBurnedGoal", "goal_active_energy"),
                    ("appleExerciseTimeGoal", "goal_exercise_minutes"),
                    ("appleStandHoursGoal", "goal_stand_hours"),
                ):
                    value = _float(elem.get(attr) or "")
                    if value and value > 0:
                        goals[goal_key] = max(goals.get(goal_key, 0.0), value)
            elem.clear()
            continue

        if tag == "Me":
            report.profile = {
                "date_of_birth": elem.get("HKCharacteristicTypeIdentifierDateOfBirth") or "",
                "biological_sex": elem.get("HKCharacteristicTypeIdentifierBiologicalSex") or "",
                "blood_type": elem.get("HKCharacteristicTypeIdentifierBloodType") or "",
                "skin_type": elem.get("HKCharacteristicTypeIdentifierFitzpatrickSkinType") or "",
            }
            elem.clear()
            continue

        if tag == "ExportDate":
            report.export_date = elem.get("value") or None
            elem.clear()
            continue

    flush()
    if sleep_buffer:
        result = write_sleep(conn, sleep_buffer, source=export_src)
        report.sleep_spans = result.inserted or len(sleep_buffer)
    if workout_rows:
        result = write_workouts(conn, workout_rows, source=export_src)
        report.workouts = result.inserted or len(workout_rows)

    report.goals = goals
    for key, value in goals.items():
        set_meta(conn, key, _trim_float(value))
    if report.profile:
        for key, value in report.profile.items():
            if value:
                set_meta(conn, f"profile_{key}", value)
    if report.export_date:
        set_meta(conn, "last_export_date", report.export_date)
    set_meta(conn, "last_export_path", str(export_path))
    conn.commit()
    return report


def _in_window(ts: int, since_ts: Optional[int], until_ts: Optional[int]) -> bool:
    if since_ts and ts < since_ts:
        return False
    if until_ts and ts > until_ts:
        return False
    return True


def _float(text: str) -> Optional[float]:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _trim_float(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _workout_from_element(elem: Element, default_source: str) -> Optional[Workout]:
    start = parse_ts(elem.get("startDate"))
    end = parse_ts(elem.get("endDate"))
    if start is None:
        return None
    if end is None:
        duration = _float(elem.get("duration") or "")
        unit = (elem.get("durationUnit") or "min").lower()
        if duration is None:
            return None
        seconds = duration * 60.0 if unit.startswith("min") else duration
        end = (start[0] + int(seconds), start[1])

    activity_raw = elem.get("workoutActivityType") or "other"
    fields: Dict[str, float] = {}
    metadata: Dict[str, str] = {}
    for child in elem:
        if child.tag == "WorkoutStatistics":
            stat_type = slug(child.get("type") or "")
            target = _STAT_TYPES.get(stat_type) or _STAT_TYPES.get(compact(stat_type))
            if not target:
                continue
            field_name, factor = target
            if field_name == "heart_rate":
                value = _float(child.get("average") or "")
                maximum = _float(child.get("maximum") or "")
                if value is not None:
                    fields["avg_hr"] = value
                if maximum is not None:
                    fields["max_hr"] = maximum
                continue
            value = child.get("sum") or child.get("average") or child.get("minimum") or child.get("maximum")
            number = _float(value or "")
            if number is None:
                continue
            fields[field_name] = number * factor
        elif child.tag == "MetadataEntry":
            key = child.get("key") or ""
            if key in ("WHOOP Strain", "HKIndoorWorkout", "HKAverageMETs", "HKWeatherTemperature"):
                metadata[key] = child.get("value") or ""

    return Workout(
        activity=activity_slug(activity_raw),
        label=activity_raw,
        start_ts=start[0],
        end_ts=end[0],
        source=(elem.get("sourceName") or default_source).strip() or default_source,
        energy_kcal=fields.get("energy_kcal"),
        distance_m=fields.get("distance_m"),
        avg_hr=fields.get("avg_hr"),
        max_hr=fields.get("max_hr"),
        metadata=metadata,
    )
