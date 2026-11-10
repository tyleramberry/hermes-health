"""Parse whatever the phone sends into samples, sleep spans, and workouts.

Two shapes are accepted on the same endpoint, because the free path and the
paid one are both first-class:

**Shortcuts** - a flat object of metric -> text rows (what ``Combine Text``
produces), or metric -> number, or metric -> list of points::

    {"steps": "2026-11-09T08:00:00-07:00 | 8123",
     "resting_heart_rate": "2026-11-09T06:00:00-07:00 | 52",
     "sleep": "2026-11-08T23:10:00-07:00 | 2026-11-09T06:35:00-07:00 | Core"}

**Health Auto Export REST v2** - ``data.metrics`` / ``data.workouts``::

    {"data": {"metrics": [{"name": "step_count", "units": "count",
                           "data": [{"date": "2026-11-09 00:00:00 -0700", "qty": 8123}]}]}}

Nothing here raises on bad input: unparseable points are counted and reported,
so one malformed shortcut row never loses the rest of a delivery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config
from .metrics import (
    SLEEP_KEY,
    WORKOUT_KEY,
    activity_slug,
    is_sleep,
    resolve,
    sleep_stage,
    slug,
)
from .store import SleepSpan, Sample, Workout

# Keys that are delivery metadata rather than metric names.
_RESERVED = {
    "source",
    "device",
    "token",
    "tz",
    "timezone",
    "units",
    "unit",
    "version",
    "schema",
    "datetime",
    "timestamp",
    "exported_at",
    "data",
    "metrics",
    "sleep",
    "workouts",
    "workout",
    "count",
    "id",
}

_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")


@dataclass
class Batch:
    samples: List[Sample] = field(default_factory=list)
    sleep: List[SleepSpan] = field(default_factory=list)
    workouts: List[Workout] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    shape: str = "shortcuts"

    def extend(self, other: "Batch") -> None:
        self.samples.extend(other.samples)
        self.sleep.extend(other.sleep)
        self.workouts.extend(other.workouts)
        self.errors.extend(other.errors)

    @property
    def points(self) -> int:
        return len(self.samples) + len(self.sleep) + len(self.workouts)

    def summary(self) -> Dict[str, Any]:
        """Counts per canonical metric key, so a receipt reads the same whichever
        spelling the phone sent."""
        seen: Dict[str, int] = {}
        for sample in self.samples:
            key = resolve(sample.metric).key
            seen[key] = seen.get(key, 0) + 1
        for span in self.sleep:
            seen["sleep"] = seen.get("sleep", 0) + 1
        for _ in self.workouts:
            seen["workouts"] = seen.get("workouts", 0) + 1
        return {
            "shape": self.shape,
            "samples": len(self.samples),
            "sleep_spans": len(self.sleep),
            "workouts": len(self.workouts),
            "metrics": seen,
            "errors": self.errors[:20],
            "error_count": len(self.errors),
        }


# ── primitives ──────────────────────────────────────────────────────────────

_ISO_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S %z",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)


def parse_ts(value: Any) -> Optional[Tuple[int, Optional[int]]]:
    """Timestamp -> ``(epoch_seconds, utc_offset_seconds_or_None)``.

    Epoch numbers are accepted too, in seconds or milliseconds. A string with
    no offset keeps ``None`` so the caller can attribute the day with the
    configured timezone instead of guessing UTC.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:  # milliseconds
            number = number / 1000.0
        if number > 1e9:  # plausible epoch seconds
            return int(number), None
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_ts(int(text))
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        offset = int(parsed.utcoffset().total_seconds()) if parsed.utcoffset() is not None else None
        return int(parsed.timestamp()), offset
    except ValueError:
        pass
    normalized = re.sub(r"\s+", " ", text)
    for fmt in _ISO_FORMATS:
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=config.tzinfo())
            return int(parsed.timestamp()), None
        offset = int(parsed.utcoffset().total_seconds()) if parsed.utcoffset() else None
        return int(parsed.timestamp()), offset
    return None


def parse_value(value: Any) -> Optional[float]:
    """Number from ``8123``, ``"8,123 steps"``, ``{"qty": 8123, "units": "count"}``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("qty", "value", "Quantity", "Avg", "average", "amount", "total"):
            if key in value:
                return parse_value(value[key])
        return None
    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            parsed = parse_value(item)
            if parsed is not None:
                return parsed
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace(",", "") if re.fullmatch(r"[-+]?[\d,]+(\.\d+)?", text.replace(" ", "")) else text
    match = _NUM.search(cleaned)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _split_rows(text: str) -> List[str]:
    parts = [line.strip() for line in re.split(r"[\r\n;]+", text)]
    return [p for p in parts if p]


def _split_fields(row: str) -> List[str]:
    """Fields of one row: pipe- or tab-separated, or ``timestamp, value``.

    A comma only separates when the row actually looks like
    ``<timestamp>, <value>`` — otherwise ``1,234 steps`` would be read as two
    fields and the sample would be lost.
    """
    if "|" in row:
        return [f.strip() for f in row.split("|")]
    if "\t" in row:
        return [f.strip() for f in row.split("\t")]
    if "," in row:
        head, tail = row.split(",", 1)
        if parse_ts(head) is not None:
            return [head.strip(), tail.strip()]
    return [row.strip()]


# ── metric values ───────────────────────────────────────────────────────────


def _points_from_metric(name: str, payload: Any, default_source: str, batch: Batch) -> None:
    """One metric's payload -> samples (or sleep spans when the metric is sleep)."""
    sleepish = is_sleep(name)
    now = int(datetime.now(timezone.utc).timestamp())

    if isinstance(payload, str):
        rows = _split_rows(payload)
        if not rows:
            return
        for row in rows:
            fields = _split_fields(row)
            if sleepish:
                _sleep_from_row(fields, default_source, batch)
                continue
            if len(fields) == 1:
                value = parse_value(fields[0])
                if value is None:
                    batch.errors.append(f"{name}: cannot read value from {row!r}")
                    continue
                batch.samples.append(Sample(name, now, value, source=default_source))
                continue
            stamp = parse_ts(fields[0])
            value = parse_value(fields[-1])
            if stamp is None or value is None:
                batch.errors.append(f"{name}: cannot read {row!r}")
                continue
            batch.samples.append(Sample(name, stamp[0], value, source=default_source, offset_s=stamp[1]))
        return

    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        batch.samples.append(Sample(name, now, float(payload), source=default_source))
        return

    if isinstance(payload, dict):
        # A single point, or HAE's {"data": [...]} / {"points": [...]} wrapper.
        for key in ("data", "points", "samples", "series", "values"):
            if key in payload and isinstance(payload[key], (list, dict)):
                _points_from_metric(name, payload[key], default_source, batch)
                return
        stamp = None
        for key in ("date", "startDate", "start", "timestamp", "ts", "time", "datetime"):
            if key in payload:
                stamp = parse_ts(payload[key])
                if stamp:
                    break
        value = parse_value(payload)
        if value is None:
            batch.errors.append(f"{name}: no value in {sorted(payload)[:6]}")
            return
        if stamp is None:
            batch.samples.append(Sample(name, now, value, source=default_source))
        else:
            batch.samples.append(Sample(name, stamp[0], value, source=default_source, offset_s=stamp[1]))
        return

    if isinstance(payload, (list, tuple)):
        for item in payload:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                stamp = parse_ts(item[0])
                value = parse_value(item[-1])
                if stamp is None or value is None:
                    batch.errors.append(f"{name}: cannot read {item!r}")
                    continue
                if sleepish:
                    _sleep_from_row([str(item[0]), str(item[1])] + [str(x) for x in item[2:]], default_source, batch)
                else:
                    batch.samples.append(Sample(name, stamp[0], value, source=default_source, offset_s=stamp[1]))
                continue
            _points_from_metric(name, item, default_source, batch)
        return

    batch.errors.append(f"{name}: unsupported payload type {type(payload).__name__}")


def _sleep_from_row(fields: Sequence[str], source: str, batch: Batch) -> None:
    """``start | end | stage`` (or ``start | hours``) -> a sleep span."""
    if not fields:
        return
    start = parse_ts(fields[0])
    if start is None:
        batch.errors.append(f"sleep: cannot read start from {fields[0]!r}")
        return
    end = None
    duration: Optional[float] = None
    stage_text = ""
    if len(fields) >= 3:
        end = parse_ts(fields[1])
        stage_text = fields[2]
        if end is None:
            hours = parse_value(fields[1])
            if hours is not None:
                duration = hours * 3600.0
    elif len(fields) == 2:
        end = parse_ts(fields[1])
        if end is None:
            hours = parse_value(fields[1])
            if hours is not None:
                duration = hours * 3600.0
    if end is None and duration is None:
        batch.errors.append(f"sleep: cannot read end/duration from {fields!r}")
        return
    stage = sleep_stage(stage_text) if stage_text else "asleep"
    end_ts = end[0] if end else start[0] + int(duration or 0)
    batch.sleep.append(
        SleepSpan(
            start_ts=start[0],
            end_ts=end_ts,
            stage=stage or "asleep",
            source=source,
            duration_s=duration,
        )
    )


# ── workouts ────────────────────────────────────────────────────────────────

_WORKOUT_ACTIVITY_KEYS = (
    "activity",
    "activityType",
    "workoutActivityType",
    "type",
    "name",
    "workoutName",
    "sport",
)
_WORKOUT_START_KEYS = ("start", "startDate", "start_time", "begin", "startTime")
_WORKOUT_END_KEYS = ("end", "endDate", "end_time", "finish", "endTime")
_WORKOUT_ENERGY_KEYS = (
    "activeEnergyBurned",
    "activeEnergy",
    "energy",
    "calories",
    "totalEnergy",
    "activeEnergyBurned_kcal",
    "kcal",
)
_WORKOUT_DISTANCE_KEYS = ("distance", "totalDistance", "distance_m", "DistanceWalkingRunning")
_WORKOUT_AVG_HR_KEYS = ("avgHeartRate", "averageHeartRate", "heartRateAvg", "avg_hr")
_WORKOUT_MAX_HR_KEYS = ("maxHeartRate", "heartRateMax", "max_hr")


def _first(mapping: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _quantity(mapping: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[float], str]:
    """A quantity plus its unit, which may be nested (``{"qty": 12.4, "units": "mi"}``)."""
    for key in keys:
        if key not in mapping or mapping[key] in (None, ""):
            continue
        raw = mapping[key]
        if isinstance(raw, dict):
            unit = raw.get("units") or raw.get("unit") or ""
            return parse_value(raw), str(unit)
        return parse_value(raw), ""
    return None, ""


def _workout_from_dict(entry: Dict[str, Any], source: str, batch: Batch) -> None:
    activity_raw = _first(entry, _WORKOUT_ACTIVITY_KEYS)
    start = parse_ts(_first(entry, _WORKOUT_START_KEYS))
    end = parse_ts(_first(entry, _WORKOUT_END_KEYS))
    if start is None:
        # HAE sometimes nests the range under "workout" / "data"
        for key in ("data", "workout", "segments"):
            nested = entry.get(key)
            if isinstance(nested, dict):
                return _workout_from_dict(nested, source, batch)
            if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                return _workout_from_dict(nested[0], source, batch)
        batch.errors.append("workouts: no start time")
        return
    if end is None:
        duration_min = parse_value(_first(entry, ("duration", "durationMinutes", "elapsed", "minutes")))
        if duration_min is None:
            batch.errors.append("workouts: no end time or duration")
            return
        end = (start[0] + int(float(duration_min) * 60), start[1])

    energy, energy_unit = _quantity(entry, _WORKOUT_ENERGY_KEYS)
    if energy is not None and (energy_unit or "").lower() in ("kj", "kilojoule", "kilojoules"):
        energy = energy / 4.184  # Health Auto Export can send kJ; the store is kcal
    distance, distance_unit = _quantity(entry, _WORKOUT_DISTANCE_KEYS)
    avg_hr = parse_value(_first(entry, _WORKOUT_AVG_HR_KEYS))
    max_hr = parse_value(_first(entry, _WORKOUT_MAX_HR_KEYS))
    if avg_hr is None or max_hr is None:
        hr = entry.get("heartRateData") or entry.get("heartRate")
        if isinstance(hr, list) and hr:
            numbers = [parse_value(point) for point in hr]
            numbers = [n for n in numbers if n is not None]
            if numbers:
                avg_hr = avg_hr if avg_hr is not None else sum(numbers) / len(numbers)
                max_hr = max_hr if max_hr is not None else max(numbers)
        elif isinstance(hr, dict):
            avg_hr = avg_hr if avg_hr is not None else parse_value(hr.get("avg"))
            max_hr = max_hr if max_hr is not None else parse_value(hr.get("max"))

    distance_m = _to_meters(distance, distance_unit or str(_first(entry, ("distanceUnit", "units")) or ""))
    label = str(activity_raw) if activity_raw is not None else "other"
    batch.workouts.append(
        Workout(
            activity=activity_slug(label),
            label=label,
            start_ts=start[0],
            end_ts=end[0],
            source=source,
            energy_kcal=energy,
            distance_m=distance_m,
            avg_hr=avg_hr,
            max_hr=max_hr,
            id=str(entry.get("id")) if entry.get("id") else None,
        )
    )


def _to_meters(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    unit = (unit or "").lower()
    if not unit:
        return float(value)
    if unit in {"km", "kilometer", "kilometers"}:
        return float(value) * 1000.0
    if unit in {"mi", "mile", "miles"}:
        return float(value) * 1609.344
    if unit in {"yd", "yard", "yards"}:
        return float(value) * 0.9144
    if unit in {"ft", "foot", "feet"}:
        return float(value) * 0.3048
    return float(value)


# ── whole payloads ──────────────────────────────────────────────────────────


def parse_payload(payload: Any, *, source: str = "") -> Batch:
    """Parse either supported delivery shape into one batch."""
    batch = Batch()
    src = source or config.default_source()
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, dict):
                    _workout_from_dict(entry, src, batch)
                else:
                    batch.errors.append(f"unsupported list entry: {type(entry).__name__}")
            batch.shape = "list"
            return batch
        batch.errors.append(f"unsupported payload type {type(payload).__name__}")
        return batch

    # HAE: {"data": {"metrics": [...], "workouts": [...]}} — also tolerate the
    # inner object arriving at the top level.
    body = payload
    if isinstance(payload.get("data"), dict) and any(
        key in payload["data"] for key in ("metrics", "workouts", "sleep", "daily")
    ):
        body = payload["data"]
        batch.shape = "hae"
    elif "metrics" in payload and isinstance(payload.get("metrics"), list):
        batch.shape = "hae"

    metrics_raw = body.get("metrics")
    if isinstance(metrics_raw, list):
        for entry in metrics_raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("metric") or entry.get("type") or entry.get("identifier")
            if not name:
                batch.errors.append("hae metric without a name")
                continue
            unit = str(entry.get("units") or entry.get("unit") or "")
            points = entry.get("data")
            if points is None:
                points = entry.get("points")
            if points is None:
                points = entry.get("values")
            if points is None:
                points = {k: v for k, v in entry.items() if k not in ("name", "units", "unit", "type")}
            _points_from_metric(str(name), points, src, batch)
    elif isinstance(metrics_raw, dict):
        for name, points in metrics_raw.items():
            _points_from_metric(str(name), points, src, batch)

    # Workouts arrive either next to HAE's metrics or as a top-level Shortcuts
    # key. Handle them in exactly one place so a body is never double-counted.
    workouts_raw = body.get("workouts", body.get("workout"))
    if workouts_raw is not None:
        entries = workouts_raw if isinstance(workouts_raw, list) else [workouts_raw]
        for entry in entries:
            if isinstance(entry, dict):
                _workout_from_dict(entry, src, batch)

    # Shortcuts: every other top-level key is a metric, a sleep block, or metadata.
    for key, value in body.items():
        if key in ("metrics", "daily", "meta", "workouts", "workout"):
            continue
        if key in ("sleep", "sleep_analysis", "sleepAnalysis"):
            if isinstance(value, dict) and "data" in value:
                value = value["data"]
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        _points_from_metric("sleep", entry, src, batch)
                    else:
                        _sleep_from_row(_split_fields(str(entry)), src, batch)
            elif isinstance(value, str):
                for row in _split_rows(value):
                    _sleep_from_row(_split_fields(row), src, batch)
            else:
                batch.errors.append("sleep: unsupported payload shape")
            continue
        if slug(key) in _RESERVED or not key.strip():
            continue
        if isinstance(value, dict) and not any(
            k in value for k in ("date", "start", "startDate", "qty", "value", "data", "points")
        ):
            batch.errors.append(f"{key}: unrecognized object payload")
            continue
        _points_from_metric(key, value, src, batch)

    if batch.shape == "shortcuts" and "metrics" in body:
        batch.shape = "hae"

    _assign_units(batch, body)
    return batch


def _count_points(points: Any) -> int:
    if isinstance(points, list):
        return len(points)
    return 0


def _assign_units(batch: Batch, body: Dict[str, Any]) -> None:
    """Carry a scalar unit from HAE's ``data.metrics[].units`` onto its samples."""
    metrics_raw = body.get("metrics")
    if not isinstance(metrics_raw, list):
        return
    for entry in metrics_raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("metric") or entry.get("type")
        unit = entry.get("units") or entry.get("unit")
        if not name or not unit:
            continue
        key = slug(str(name))
        for sample in batch.samples:
            if slug(sample.metric) == key and not sample.unit:
                sample.unit = str(unit)


def parse_json_text(raw: str, *, source: str = "") -> Batch:
    """Parse a raw request body. Never raises: a bad body comes back as errors."""
    import json

    text = (raw or "").strip()
    if not text:
        return Batch(errors=["empty body"])
    try:
        payload = json.loads(text)
    except ValueError as exc:
        # Tolerate form-encoded bodies from a shortcut that builds fields manually.
        from urllib.parse import parse_qs

        parsed = parse_qs(text, keep_blank_values=True)
        if len(parsed) > 1:
            payload = {k: v[0] for k, v in parsed.items()}
        else:
            return Batch(errors=[f"invalid JSON: {exc}"])
    return parse_payload(payload, source=source)
