"""Read side: the answers an agent actually asks for.

Everything returns plain JSON-serializable dicts with local dates, so a tool
handler, an MCP response, a dashboard and a cron brief can all share it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config
from .metrics import AVG, LAST, SUM, METRICS, resolve
from .store import counts, cursor, day_of, get_meta, last_ingest, meta_map, sample_range

RING_ACTIVE_ENERGY = 500.0  # move goal default; read from meta when known
RING_EXERCISE = 30.0
RING_STAND = 12.0


# ── small helpers ───────────────────────────────────────────────────────────


def today_local() -> str:
    return datetime.now(config.tzinfo()).date().isoformat()


def day_shift(day: str, delta: int) -> str:
    parsed = date.fromisoformat(day)
    return (parsed + timedelta(days=delta)).isoformat()


def fmt_hours(hours: Optional[float]) -> Optional[str]:
    if hours is None:
        return None
    total_minutes = int(round(float(hours) * 60))
    sign = "-" if total_minutes < 0 else ""
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60}h{total_minutes % 60:02d}m"


def fmt_clock(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), config.tzinfo()).strftime("%H:%M")


def round_or_none(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def mean(values: Sequence[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def trend_of(values: Sequence[float]) -> Dict[str, Any]:
    """Direction of a series: second half vs first half, with a % change."""
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 4:
        return {"direction": "flat", "change_pct": None, "points": len(clean)}
    half = len(clean) // 2
    first = mean(clean[:half])
    second = mean(clean[half:])
    if first is None or second is None or first == 0:
        return {"direction": "flat", "change_pct": None, "points": len(clean)}
    change = (second - first) / abs(first) * 100.0
    direction = "flat"
    if change >= 5:
        direction = "up"
    elif change <= -5:
        direction = "down"
    return {"direction": direction, "change_pct": round(change, 1), "points": len(clean)}


# ── series ──────────────────────────────────────────────────────────────────


def daily_series(
    conn: sqlite3.Connection,
    metric: str,
    *,
    days: int = 30,
    start_day: Optional[str] = None,
    end_day: Optional[str] = None,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Daily values for one metric, oldest first, with weeks of history to spare."""
    key = resolve(metric).key
    end = end_day or today_local()
    begin = start_day or day_shift(end, -(max(1, int(days)) - 1))
    sql = (
        "SELECT day, source, unit, value, val_n, val_min, val_max FROM daily"
        " WHERE metric=? AND day>=? AND day<=?"
    )
    params: List[Any] = [key, begin, end]
    if source:
        sql += " AND source=?"
        params.append(source)
    sql += " ORDER BY day"
    rows = conn.execute(sql, params).fetchall()

    # A metric delivered from two phones would otherwise produce two rows a day.
    merged: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        entry = merged.setdefault(
            row["day"],
            {
                "day": row["day"],
                "value": 0.0,
                "unit": row["unit"],
                "samples": 0,
                "min": row["val_min"],
                "max": row["val_max"],
                "sources": [],
            },
        )
        entry["value"] += float(row["value"])
        entry["samples"] += int(row["val_n"] or 0)
        entry["sources"].append(row["source"])
        if row["val_min"] is not None:
            entry["min"] = row["val_min"] if entry["min"] is None else min(entry["min"], row["val_min"])
        if row["val_max"] is not None:
            entry["max"] = row["val_max"] if entry["max"] is None else max(entry["max"], row["val_max"])
    out = []
    for day in sorted(merged):
        entry = merged[day]
        entry["value"] = round_or_none(entry["value"], 2)
        entry["min"] = round_or_none(entry["min"], 2)
        entry["max"] = round_or_none(entry["max"], 2)
        out.append(entry)
    return out


def metric_value(conn: sqlite3.Connection, metric: str, day: Optional[str] = None) -> Optional[Dict[str, Any]]:
    series = daily_series(conn, metric, days=1, start_day=day, end_day=day)
    return series[0] if series else None


def window_stats(
    conn: sqlite3.Connection, metric: str, *, days: int, end_day: Optional[str] = None
) -> Dict[str, Any]:
    """Average / total / coverage for the last ``days`` calendar days."""
    end = end_day or today_local()
    begin = day_shift(end, -(max(1, int(days)) - 1))
    series = daily_series(conn, metric, start_day=begin, end_day=end)
    values = [e["value"] for e in series if e["value"] is not None]
    obj = resolve(metric)
    if not values:
        return {"metric": obj.key, "label": obj.label, "unit": obj.unit, "days": days, "n": 0, "value": None}
    total = round_or_none(sum(values), 2)
    average = round_or_none(mean(values), 2)
    return {
        "metric": obj.key,
        "label": obj.label,
        "unit": obj.unit,
        "aggregation": obj.agg,
        "days": days,
        "n": len(values),
        "average": average,
        "total": total if obj.agg == SUM else None,
        "latest": round_or_none(values[-1], 2),
        "latest_day": series[-1]["day"],
        "min": round_or_none(min(values), 2),
        "max": round_or_none(max(values), 2),
        "trend": trend_of(values),
    }


def compare_windows(
    conn: sqlite3.Connection, metric: str, days: int = 7, end_day: Optional[str] = None
) -> Dict[str, Any]:
    """This window vs the one before it — the honest way to say "better"."""
    obj = resolve(metric)
    end = end_day or today_local()
    recent = daily_series(conn, metric, start_day=day_shift(end, -(days - 1)), end_day=end)
    prior_end = day_shift(end, -days)
    prior = daily_series(conn, metric, start_day=day_shift(prior_end, -(days - 1)), end_day=prior_end)
    recent_values = [e["value"] for e in recent if e["value"] is not None]
    prior_values = [e["value"] for e in prior if e["value"] is not None]
    recent_avg = mean(recent_values)
    prior_avg = mean(prior_values)
    change_pct = None
    if recent_avg is not None and prior_avg:
        change_pct = round((recent_avg - prior_avg) / abs(prior_avg) * 100.0, 1)
    return {
        "metric": obj.key,
        "label": obj.label,
        "unit": obj.unit,
        "days": days,
        "recent_average": round_or_none(recent_avg, 2),
        "previous_average": round_or_none(prior_avg, 2),
        "change_pct": change_pct,
        "direction": "flat"
        if change_pct is None or abs(change_pct) < 5
        else ("up" if change_pct > 0 else "down"),
        "recent_days": len(recent_values),
        "previous_days": len(prior_values),
    }


# ── sleep ───────────────────────────────────────────────────────────────────


@dataclass
class _Span:
    start: int
    end: int
    stage: str
    duration: float


def _sleep_spans(conn: sqlite3.Connection, start_ts: int) -> List[_Span]:
    rows = conn.execute(
        "SELECT start_ts, end_ts, stage, duration_s FROM sleep WHERE end_ts >= ? ORDER BY start_ts, end_ts",
        (start_ts,),
    ).fetchall()
    return [_Span(r["start_ts"], r["end_ts"], r["stage"], float(r["duration_s"] or 0)) for r in rows]


def sleep_nights(
    conn: sqlite3.Connection,
    *,
    days: int = 14,
    end_day: Optional[str] = None,
    gap_hours: float = 2.0,
    min_asleep_hours: float = 2.0,
) -> List[Dict[str, Any]]:
    """Cluster sleep intervals into nights, attributed to the wake-up day.

    ``In Bed`` overlaps the asleep stages, so it is reported separately and
    never summed into time asleep. A session with less than
    ``min_asleep_hours`` asleep is flagged ``is_nap`` rather than dropped —
    naps are real, they are just not "last night".
    """
    end_day = end_day or today_local()
    begin_day = day_shift(end_day, -(max(1, int(days)) - 1))
    start_ts = int(
        datetime.fromisoformat(begin_day).replace(tzinfo=config.tzinfo()).timestamp()
    ) - 6 * 3600  # a night that begins the evening before still counts
    spans = _sleep_spans(conn, start_ts)
    if not spans:
        return []

    spans.sort(key=lambda s: (s.start, s.end))
    sessions: List[List[_Span]] = [[spans[0]]]
    for span in spans[1:]:
        last_end = max(s.end for s in sessions[-1])
        if span.start - last_end > gap_hours * 3600:
            sessions.append([span])
        else:
            sessions[-1].append(span)

    nights: List[Dict[str, Any]] = []
    for session in sessions:
        start = min(s.start for s in session)
        end = max(s.end for s in session)
        stages: Dict[str, float] = {}
        for span in session:
            stages[span.stage] = stages.get(span.stage, 0.0) + span.duration
        asleep_s = stages.get("core", 0.0) + stages.get("deep", 0.0) + stages.get("rem", 0.0) + stages.get("asleep", 0.0)
        awake_s = stages.get("awake", 0.0)
        in_bed_s = stages.get("in_bed", 0.0)
        elapsed = max(float(end - start), 0.0)
        day = day_of(end, None, config.tzinfo())
        if day < begin_day or day > end_day:
            continue
        nights.append(
            {
                "day": day,
                "weekday": datetime.fromisoformat(day).strftime("%a"),
                "bedtime": fmt_clock(start),
                "wake": fmt_clock(end),
                "bedtime_ts": start,
                "wake_ts": end,
                "asleep_hours": round(asleep_s / 3600.0, 2),
                "asleep": fmt_hours(asleep_s / 3600.0),
                "in_bed_hours": round((in_bed_s or elapsed) / 3600.0, 2),
                "awake_hours": round(awake_s / 3600.0, 2),
                "core_hours": round(stages.get("core", 0.0) / 3600.0, 2),
                "deep_hours": round(stages.get("deep", 0.0) / 3600.0, 2),
                "rem_hours": round(stages.get("rem", 0.0) / 3600.0, 2),
                "unspecified_hours": round(stages.get("asleep", 0.0) / 3600.0, 2),
                "efficiency_pct": round(asleep_s / elapsed * 100.0, 1) if elapsed else None,
                "is_nap": (asleep_s / 3600.0) < min_asleep_hours,
                "interruptions": len([s for s in session if s.stage == "awake"]),
            }
        )
    nights.sort(key=lambda n: n["day"])
    return nights


def sleep_report(
    conn: sqlite3.Connection, *, days: int = 14, end_day: Optional[str] = None
) -> Dict[str, Any]:
    """Nights plus the averages a brief quotes."""
    nights = sleep_nights(conn, days=days, end_day=end_day)
    main = [n for n in nights if not n["is_nap"]]
    recent = main[-7:]
    prior = main[-14:-7]
    asleep = [n["asleep_hours"] for n in recent]
    deep = [n["deep_hours"] for n in recent]
    rem = [n["rem_hours"] for n in recent]
    efficiency = [n["efficiency_pct"] for n in recent if n["efficiency_pct"] is not None]
    bed_minutes = _clock_minutes([n["bedtime"] for n in recent])
    wake_minutes = _clock_minutes([n["wake"] for n in recent])
    return {
        "days": days,
        "nights": nights,
        "last_night": main[-1] if main else None,
        "averages": {
            "window": f"last {len(recent)} nights",
            "asleep_hours": round_or_none(mean(asleep), 2),
            "asleep": fmt_hours(mean(asleep)),
            "deep_hours": round_or_none(mean(deep), 2),
            "rem_hours": round_or_none(mean(rem), 2),
            "efficiency_pct": round_or_none(mean(efficiency), 1),
            "bedtime": _avg_clock(bed_minutes),
            "wake": _avg_clock(wake_minutes),
            "variability_minutes": round_or_none(_stdev(asleep) * 60, 0) if len(asleep) > 1 else None,
        },
        "previous_average_asleep_hours": round_or_none(mean([n["asleep_hours"] for n in prior]), 2),
        "naps": [n for n in nights if n["is_nap"]],
        "consistency": {
            "bedtime_sd_minutes": round_or_none(_stdev(bed_minutes), 0)
            if len(bed_minutes) > 1
            else None,
            "wake_sd_minutes": round_or_none(_stdev(wake_minutes), 0) if len(wake_minutes) > 1 else None,
        },
    }


def _clock_minutes(values: Iterable[Optional[str]]) -> List[float]:
    """Clock strings -> minutes from midnight, unwrapping past-midnight bedtimes."""
    out: List[float] = []
    for value in values:
        if not value:
            continue
        try:
            hours, minutes = value.split(":")
            total = int(hours) * 60 + int(minutes)
        except (AttributeError, ValueError):
            continue
        if total < 12 * 60:
            total += 24 * 60  # bedtimes after midnight sort with the previous evening
        out.append(float(total))
    return out


def _avg_clock(minutes: Sequence[float]) -> Optional[str]:
    if not minutes:
        return None
    average = sum(minutes) / len(minutes)
    average = int(round(average)) % (24 * 60)
    return f"{average // 60:02d}:{average % 60:02d}"


def _stdev(values: Sequence[float]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 2:
        return None
    avg = sum(clean) / len(clean)
    variance = sum((v - avg) ** 2 for v in clean) / (len(clean) - 1)
    return variance ** 0.5


# ── workouts ────────────────────────────────────────────────────────────────


def workouts_recent(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    activity: Optional[str] = None,
    limit: int = 50,
    end_day: Optional[str] = None,
) -> List[Dict[str, Any]]:
    end_day = end_day or today_local()
    begin_day = day_shift(end_day, -(max(1, int(days)) - 1))
    sql = (
        "SELECT id, activity, start_ts, end_ts, duration_s, energy_kcal, distance_m, avg_hr, max_hr, source, day"
        " FROM workouts WHERE day >= ? AND day <= ?"
    )
    params: List[Any] = [begin_day, end_day]
    if activity:
        sql += " AND activity = ?"
        params.append(activity)
    sql += " ORDER BY start_ts DESC LIMIT ?"
    params.append(int(limit))
    out = []
    for row in conn.execute(sql, params):
        out.append(
            {
                "id": row["id"],
                "activity": row["activity"],
                "day": row["day"],
                "start": fmt_clock(row["start_ts"]),
                "end": fmt_clock(row["end_ts"]),
                "start_ts": row["start_ts"],
                "duration_minutes": round_or_none(float(row["duration_s"] or 0) / 60.0, 1),
                "energy_kcal": round_or_none(row["energy_kcal"], 1),
                "distance_mi": round_or_none(
                    float(row["distance_m"]) / 1609.344 if row["distance_m"] else None, 2
                ),
                "avg_hr": round_or_none(row["avg_hr"], 0),
                "max_hr": round_or_none(row["max_hr"], 0),
                "source": row["source"],
            }
        )
    return out


def workout_report(
    conn: sqlite3.Connection, *, days: int = 30, end_day: Optional[str] = None
) -> Dict[str, Any]:
    sessions = workouts_recent(conn, days=days, limit=200, end_day=end_day)
    by_activity: Dict[str, Dict[str, Any]] = {}
    for session in sessions:
        bucket = by_activity.setdefault(
            session["activity"], {"activity": session["activity"], "sessions": 0, "minutes": 0.0, "kcal": 0.0}
        )
        bucket["sessions"] += 1
        bucket["minutes"] = round(bucket["minutes"] + (session["duration_minutes"] or 0), 1)
        bucket["kcal"] = round(bucket["kcal"] + (session["energy_kcal"] or 0), 1)
    first_ts = min((s["start_ts"] for s in sessions), default=None)
    return {
        "days": days,
        "n": len(sessions),
        "total_minutes": round_or_none(sum(s["duration_minutes"] or 0 for s in sessions), 1),
        "total_kcal": round_or_none(sum(s["energy_kcal"] or 0 for s in sessions), 1),
        "by_activity": sorted(by_activity.values(), key=lambda b: -b["sessions"]),
        "sessions": sessions,
        "rest_days": _rest_days(conn, days=days, end_day=end_day),
        "source_note": None
        if sessions
        else "No workouts stored. Shortcuts cannot read workouts: import the Health export or send them from Health Auto Export.",
        "last_workout_ts": first_ts,
    }


def _rest_days(conn: sqlite3.Connection, *, days: int, end_day: Optional[str] = None) -> Optional[int]:
    end_day = end_day or today_local()
    begin = day_shift(end_day, -(days - 1))
    row = conn.execute(
        "SELECT COUNT(DISTINCT day) AS n FROM workouts WHERE day >= ? AND day <= ?", (begin, end_day)
    ).fetchone()
    if row is None:
        return None
    return max(0, days - int(row["n"] or 0))


# ── rings / today ───────────────────────────────────────────────────────────


def rings(conn: sqlite3.Connection, day: Optional[str] = None) -> Dict[str, Any]:
    """Today's three rings, straight from the phone if it sends them."""
    target_day = day or today_local()
    goals = {
        "active_energy": float(get_meta(conn, "goal_active_energy", str(RING_ACTIVE_ENERGY)) or RING_ACTIVE_ENERGY),
        "exercise_minutes": float(get_meta(conn, "goal_exercise_minutes", str(RING_EXERCISE)) or RING_EXERCISE),
        "stand_hours": float(get_meta(conn, "goal_stand_hours", str(RING_STAND)) or RING_STAND),
    }
    units = {"active_energy": "Cal", "exercise_minutes": "min", "stand_hours": "count"}
    out: Dict[str, Any] = {"day": target_day, "rings": {}}
    for metric, goal in goals.items():
        entry = metric_value(conn, metric, day=target_day)
        value = float(entry["value"]) if entry and entry["value"] is not None else 0.0
        out["rings"][metric] = {
            "value": round_or_none(value, 1),
            "goal": goal,
            "unit": units[metric],
            "pct": round(value / goal * 100.0, 0) if goal else None,
        }
    steps = metric_value(conn, "steps", day=target_day)
    out["steps"] = round_or_none(steps["value"], 0) if steps else None
    return out


def activity_summary(
    conn: sqlite3.Connection, *, days: int = 7, end_day: Optional[str] = None
) -> Dict[str, Any]:
    """Today's rings (with today's step count) plus the window's activity totals."""
    today_rings = rings(conn, day=end_day)
    return {
        "rings": today_rings["rings"],
        "steps_today": today_rings["steps"],
        "steps": window_stats(conn, "steps", days=days, end_day=end_day),
        "active_energy": window_stats(conn, "active_energy", days=days, end_day=end_day),
        "exercise_minutes": window_stats(conn, "exercise_minutes", days=days, end_day=end_day),
        "flights_climbed": window_stats(conn, "flights_climbed", days=days, end_day=end_day),
        "distance_walking_running": window_stats(
            conn, "distance_walking_running", days=days, end_day=end_day
        ),
    }


# ── summary ─────────────────────────────────────────────────────────────────


def summary(conn: sqlite3.Connection, *, days: int = 7, end_day: Optional[str] = None) -> Dict[str, Any]:
    """The one call a brief or a dashboard needs.

    Each block carries what it was computed from (window, n, unit) so a number
    never arrives without its context.
    """
    sleep = sleep_report(conn, days=max(7, days), end_day=end_day)
    activity = activity_summary(conn, days=days, end_day=end_day)
    workouts = workout_report(conn, days=max(7, days * 4), end_day=end_day)
    def stats(metric: str, window: int) -> Dict[str, Any]:
        return window_stats(conn, metric, days=window, end_day=end_day)

    heart = {
        "resting_heart_rate": stats("resting_heart_rate", days),
        "resting_heart_rate_28d": stats("resting_heart_rate", 28),
        "hrv": stats("hrv", days),
        "hrv_28d": stats("hrv", 28),
        "heart_rate_today": stats("heart_rate", 1),
        "vo2_max": stats("vo2_max", 90),
    }
    body = {
        "body_mass": stats("body_mass", 90),
        "sleeping_breathing_disturbances": stats("sleeping_breathing_disturbances", days),
        "oxygen_saturation": stats("oxygen_saturation", days),
        "respiratory_rate": stats("respiratory_rate", days),
        "wrist_temperature": stats("wrist_temperature", days),
    }
    changes = {
        metric: compare_windows(conn, metric, days=days, end_day=end_day)
        for metric in ("resting_heart_rate", "hrv", "steps", "active_energy")
    }
    return {
        "generated_at": datetime.now(config.tzinfo()).isoformat(timespec="seconds"),
        "window_days": days,
        "today": {"day": end_day or today_local(), **activity},
        "sleep": {
            "last_night": sleep["last_night"],
            "averages": sleep["averages"],
            "previous_average_asleep_hours": sleep["previous_average_asleep_hours"],
            "consistency": sleep["consistency"],
        },
        "heart": heart,
        "body": body,
        "workouts": {k: v for k, v in workouts.items() if k != "sessions"},
        "changes": changes,
        "data": freshness(conn),
    }


def freshness(conn: sqlite3.Connection) -> Dict[str, Any]:
    """How current the store is — the first thing to check when a number looks odd."""
    rows = last_ingest(conn, limit=1)
    first, last = sample_range(conn)
    now = int(time.time())
    newest = None
    for candidate in (
        last,
        (conn.execute("SELECT MAX(end_ts) AS t FROM sleep").fetchone() or {"t": None})["t"],
        (conn.execute("SELECT MAX(end_ts) AS t FROM workouts").fetchone() or {"t": None})["t"],
    ):
        if candidate and (newest is None or candidate > newest):
            newest = candidate
    return {
        "last_delivery": rows[0] if rows else None,
        "newest_data_ts": newest,
        "newest_data_iso": datetime.fromtimestamp(newest, config.tzinfo()).isoformat(timespec="seconds")
        if newest
        else None,
        "hours_since_data": round_or_none((now - newest) / 3600.0, 1) if newest else None,
        "first_data_ts": first,
        "timezone": config.tz_name(),
    }


def status(conn: sqlite3.Connection) -> Dict[str, Any]:
    from . import __version__

    known = sorted(
        set(_metrics_with_data(conn)),
    )
    first, last = sample_range(conn)
    db_file = get_meta(conn, "db_path") or ""
    size = os.path.getsize(db_file) if db_file and os.path.exists(db_file) else None
    return {
        "version": __version__,
        "db_path": db_file,
        "db_bytes": size,
        "timezone": config.tz_name(),
        "counts": counts(conn),
        "metrics_available": known,
        "metric_count": len(known),
        "first_sample_ts": first,
        "last_sample_ts": last,
        "freshness": freshness(conn),
        "meta": {k: v for k, v in meta_map(conn).items() if k not in ("db_path",)},
    }


def _metrics_with_data(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT DISTINCT metric FROM samples_hourly").fetchall()
    return [r["metric"] for r in rows]


def query(
    conn: sqlite3.Connection,
    metric: str,
    *,
    days: int = 30,
    start_day: Optional[str] = None,
    end_day: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """One metric, resolved to its canonical name, with the series and a trend."""
    obj = resolve(metric)
    series = daily_series(conn, obj.key, days=days, start_day=start_day, end_day=end_day, source=source)
    return {
        "metric": obj.key,
        "label": obj.label,
        "unit": obj.unit,
        "aggregation": obj.agg,
        "days": days,
        "n": len(series),
        "series": series,
        "average": round_or_none(mean([e["value"] for e in series if e["value"] is not None]), 2),
        "latest": series[-1] if series else None,
        "trend": trend_of([e["value"] for e in series if e["value"] is not None]),
    }


def catalog(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Metric keys known to the registry, plus any extra keys in this store."""
    from .metrics import describe

    stored = set(_metrics_with_data(conn))
    registry = describe()
    registered = {entry["key"] for entry in registry}
    extra = [
        {"key": key, "label": key.replace("_", " ").capitalize(), "unit": None, "aggregation": None}
        for key in sorted(stored - registered)
    ]
    return {
        "metrics": registry,
        "unregistered_in_store": extra,
        "stored_metric_count": len(stored),
    }


def search_metrics(conn: sqlite3.Connection, term: str) -> List[Dict[str, Any]]:
    from .metrics import match_keys

    stored = set(_metrics_with_data(conn))
    out = []
    for key in match_keys(term):
        obj = METRICS.get(key) or resolve(key)
        out.append(
            {
                "key": obj.key,
                "label": obj.label,
                "unit": obj.unit,
                "aggregation": obj.agg,
                "has_data": key in stored,
            }
        )
    return out
