"""SQLite store: raw samples, hourly rollups, daily rollups, sleep, workouts.

Layout
------
``samples_raw``   one row per distinct phone sample, primary key
                  ``(metric, ts, source)``. This is what makes re-sending a
                  window idempotent: the same sample twice writes once.
``samples_hourly``rollup per metric/source/hour, rebuilt from raw.
``daily``         rollup per metric/source/local day, rebuilt from hourly.
``sleep``         sleep-analysis intervals (start, end, stage).
``workouts``      workouts, which Shortcuts cannot read at all.
``ingest_log``    one row per delivery, so "did my phone sync?" is a query.
``meta``          schema version, timezone, export metadata.

Raw rows are pruned after ``raw_keep_days`` (default 120); hourly and daily
rollups keep the full history, so a year of data stays small while recent
re-sends stay exactly-once.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__, config
from .metrics import Metric, resolve

SCHEMA_VERSION = 1
DEFAULT_RAW_KEEP_DAYS = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS samples_raw (
    metric TEXT NOT NULL,
    ts     INTEGER NOT NULL,
    source TEXT NOT NULL,
    value  REAL NOT NULL,
    unit   TEXT,
    day    TEXT NOT NULL,
    agg    TEXT NOT NULL,
    PRIMARY KEY (metric, ts, source)
);
CREATE INDEX IF NOT EXISTS idx_raw_metric_day ON samples_raw (metric, day);
CREATE TABLE IF NOT EXISTS samples_hourly (
    metric  TEXT NOT NULL,
    hour_ts INTEGER NOT NULL,
    source  TEXT NOT NULL,
    day     TEXT NOT NULL,
    unit    TEXT,
    agg     TEXT NOT NULL,
    val_sum REAL NOT NULL,
    val_n   INTEGER NOT NULL,
    val_min REAL,
    val_max REAL,
    PRIMARY KEY (metric, hour_ts, source)
);
CREATE INDEX IF NOT EXISTS idx_hourly_day ON samples_hourly (metric, day);
CREATE TABLE IF NOT EXISTS daily (
    metric  TEXT NOT NULL,
    day     TEXT NOT NULL,
    source  TEXT NOT NULL,
    unit    TEXT,
    agg     TEXT NOT NULL,
    value   REAL NOT NULL,
    val_n   INTEGER NOT NULL,
    val_min REAL,
    val_max REAL,
    PRIMARY KEY (metric, day, source)
);
CREATE INDEX IF NOT EXISTS idx_daily_metric ON daily (metric, day);
CREATE TABLE IF NOT EXISTS sleep (
    start_ts   INTEGER NOT NULL,
    end_ts     INTEGER NOT NULL,
    stage      TEXT NOT NULL,
    source     TEXT NOT NULL,
    duration_s REAL NOT NULL,
    day        TEXT NOT NULL,
    PRIMARY KEY (start_ts, end_ts, stage, source)
);
CREATE INDEX IF NOT EXISTS idx_sleep_day ON sleep (day);
CREATE TABLE IF NOT EXISTS workouts (
    id          TEXT PRIMARY KEY,
    activity    TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    duration_s  REAL NOT NULL,
    energy_kcal REAL,
    distance_m  REAL,
    avg_hr      REAL,
    max_hr      REAL,
    source      TEXT NOT NULL,
    day         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workout_start ON workouts (start_ts);
CREATE TABLE IF NOT EXISTS ingest_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ts   INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    source  TEXT NOT NULL,
    rows    INTEGER NOT NULL,
    metrics TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_at ON ingest_log (at_ts);
"""


def connect(path: Optional[object] = None) -> sqlite3.Connection:
    """Open (and initialize) the store. Creates parent directories as needed."""
    db_file = Path(path) if path is not None else config.default_db_path()
    if str(db_file) != ":memory:":
        db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema(conn)
    _store_location(conn, db_file)
    return conn


def db_file_of(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row["file"] if row and row["file"] else None


def _store_location(conn: sqlite3.Connection, db_file: Path) -> None:
    set_meta(conn, "db_path", str(db_file))
    set_meta(conn, "timezone", config.tz_name())


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = get_meta(conn, "schema_version")
    if existing is None:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        set_meta(conn, "created_at", ts_now_iso())
        set_meta(conn, "hermes_health_version", __version__)
    elif int(existing) != SCHEMA_VERSION:
        # Nothing to migrate yet: v1 is the first shipped schema. A future
        # version migrates here rather than refusing to open the file.
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


# ── meta ────────────────────────────────────────────────────────────────────


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else row["value"]


def meta_map(conn: sqlite3.Connection) -> Dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}


# ── time helpers ────────────────────────────────────────────────────────────


def ts_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hour_bucket(ts: int) -> int:
    return int(ts) - (int(ts) % 3600)


def day_of(ts: int, offset_seconds: Optional[int] = None, tz=None) -> str:
    """Local calendar date for an epoch timestamp.

    A sample that carries its own UTC offset is attributed with that offset
    (what the phone thought the day was); anything else uses the configured
    timezone.
    """
    from datetime import datetime, timedelta, timezone as _tz

    if offset_seconds is not None:
        zone = _tz(timedelta(seconds=offset_seconds))
    else:
        zone = tz if tz is not None else config.tzinfo()
    return datetime.fromtimestamp(int(ts), zone).date().isoformat()


# ── writes ──────────────────────────────────────────────────────────────────


@dataclass
class Sample:
    metric: str
    ts: int
    value: float
    unit: str = ""
    source: str = ""
    offset_s: Optional[int] = None

    def metric_obj(self) -> Metric:
        return resolve(self.metric, self.unit)


@dataclass
class SleepSpan:
    start_ts: int
    end_ts: int
    stage: str
    source: str = ""
    duration_s: Optional[float] = None


@dataclass
class Workout:
    activity: str
    start_ts: int
    end_ts: int
    source: str = ""
    energy_kcal: Optional[float] = None
    distance_m: Optional[float] = None
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    id: Optional[str] = None
    #: True for Apple workout types like ``Other``; kept for honesty in briefs.
    label: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteResult:
    inserted: int = 0
    duplicates: int = 0
    skipped: int = 0
    metrics: Dict[str, int] = field(default_factory=dict)

    def add(self, metric: str, count: int) -> None:
        self.metrics[metric] = self.metrics.get(metric, 0) + count

    def as_dict(self) -> Dict[str, Any]:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "metrics": self.metrics,
        }


def write_samples(
    conn: sqlite3.Connection,
    samples: Iterable[Sample],
    *,
    source: str = "",
    raw_keep_days: int = DEFAULT_RAW_KEEP_DAYS,
) -> WriteResult:
    """Store samples, then bring the rollups they touched up to date.

    Idempotent per sample: the primary key is ``(metric, ts, source)``, so a
    retried delivery of the same window changes nothing and reports duplicates
    instead of double-counting.

    Two paths, because raw samples are pruned and rollups are not:

    * **Inside the raw window** the hour is rebuilt from its raw rows, which is
      exactly right no matter how samples arrive.
    * **Older than the raw window** there is nothing left to rebuild from, and
      the hour was already rolled up before its raw rows were pruned - so only
      the samples that actually inserted are merged in, as a delta.
    """
    result = WriteResult()
    tz = config.tzinfo()
    recent: List[Tuple[Any, ...]] = []
    #: (metric, bucket, source) -> {ts: (value, unit, day, agg)} for pruned hours
    old: Dict[Tuple[str, int, str], Dict[int, Tuple[Any, ...]]] = {}
    floor_ts = int(time.time()) - max(0, raw_keep_days) * 86400
    affected: set = set()

    for sample in samples:
        try:
            value = float(sample.value)
        except (TypeError, ValueError):
            result.skipped += 1
            continue
        if value != value:  # NaN
            result.skipped += 1
            continue
        metric = sample.metric_obj()
        src = sample.source or source or config.default_source()
        day = day_of(sample.ts, sample.offset_s, tz)
        unit = sample.unit or metric.unit
        ts = int(sample.ts)
        bucket = hour_bucket(ts)
        row = (metric.key, ts, src, value, unit, day, metric.agg)
        affected.add((metric.key, bucket, src, metric.agg, day))
        if bucket >= floor_ts:
            recent.append(row)
        else:
            old.setdefault((metric.key, bucket, src), {})[ts] = (value, unit, day, metric.agg)
        result.add(metric.key, 1)

    if not recent and not old:
        return result

    if recent:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO samples_raw (metric, ts, source, value, unit, day, agg)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            recent,
        )
        recent_inserted = conn.total_changes - before
        result.inserted += recent_inserted
        result.duplicates += len(recent) - recent_inserted

    if old:
        # One at a time, so ``rowcount`` tells us which samples are new: only a
        # genuinely new sample may be added to an already-rolled-up hour.
        for (metric, bucket, src), points in old.items():
            for ts in sorted(points):
                value, unit, day, agg = points[ts]
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO samples_raw (metric, ts, source, value, unit, day, agg)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (metric, ts, src, value, unit, day, agg),
                )
                if cursor.rowcount != 1:
                    result.duplicates += 1  # already stored; adding it would double count
                    continue
                result.inserted += 1
                conn.execute(
                    "INSERT INTO samples_hourly (metric, hour_ts, source, day, unit, agg, val_sum, val_n,"
                    " val_min, val_max) VALUES (?,?,?,?,?,?,?,1,?,?)"
                    " ON CONFLICT(metric, hour_ts, source) DO UPDATE SET"
                    " val_sum = val_sum + excluded.val_sum, val_n = val_n + 1,"
                    " val_min = MIN(COALESCE(val_min, excluded.val_min), excluded.val_min),"
                    " val_max = MAX(COALESCE(val_max, excluded.val_max), excluded.val_max)",
                    (metric, bucket, src, day, unit, agg, value, value, value),
                )

    # Pruned hours were merged above; every other touched hour is rebuilt from raw.
    _rebuild_hours(conn, {entry for entry in affected if entry[1] >= floor_ts})
    _rebuild_days(conn, {(m, d, s) for m, _h, s, _a, d in affected})
    conn.commit()
    return result


def _rebuild_hours(conn: sqlite3.Connection, affected: Iterable[Tuple[str, int, str, str, str]]) -> None:
    """Recompute hourly buckets from their raw rows.

    Callers pass only hours that still have raw rows (inside the retention
    window): a pruned hour has nothing left to recompute from and keeps the
    value it was rolled up to, with any late sample already merged by
    :func:`write_samples`. An hour whose raw rows all vanished is deleted so it
    cannot linger as a ghost bucket.
    """
    for metric, bucket, src, agg, day in affected:
        row = conn.execute(
            "SELECT SUM(value) AS s, COUNT(*) AS n, MIN(value) AS mn, MAX(value) AS mx,"
            " MAX(unit) AS unit FROM samples_raw WHERE metric=? AND ts>=? AND ts<? AND source=?",
            (metric, bucket, bucket + 3600, src),
        ).fetchone()
        if not row or not row["n"]:
            conn.execute(
                "DELETE FROM samples_hourly WHERE metric=? AND hour_ts=? AND source=?",
                (metric, bucket, src),
            )
            continue
        unit = row["unit"]
        conn.execute(
            "INSERT INTO samples_hourly (metric, hour_ts, source, day, unit, agg, val_sum, val_n,"
            " val_min, val_max) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(metric, hour_ts, source) DO UPDATE SET"
            " day=excluded.day, unit=excluded.unit, agg=excluded.agg, val_sum=excluded.val_sum,"
            " val_n=excluded.val_n, val_min=excluded.val_min, val_max=excluded.val_max",
            (
                metric,
                bucket,
                src,
                day,
                unit,
                agg,
                float(row["s"] or 0.0),
                int(row["n"] or 0),
                row["mn"],
                row["mx"],
            ),
        )


def _rebuild_days(conn: sqlite3.Connection, affected: Iterable[Tuple[str, str, str]]) -> None:
    """Recompute daily rows for each touched metric/day/source from hourly buckets."""
    for metric, day, src in affected:
        rows = conn.execute(
            "SELECT agg, unit, val_sum, val_n, val_min, val_max FROM samples_hourly"
            " WHERE metric=? AND day=? AND source=? ORDER BY hour_ts",
            (metric, day, src),
        ).fetchall()
        if not rows:
            conn.execute("DELETE FROM daily WHERE metric=? AND day=? AND source=?", (metric, day, src))
            continue
        agg = rows[0]["agg"]
        unit = next((r["unit"] for r in rows if r["unit"]), rows[0]["unit"])
        total_n = sum(int(r["val_n"] or 0) for r in rows)
        total_sum = sum(float(r["val_sum"] or 0.0) for r in rows)
        mins = [float(r["val_min"]) for r in rows if r["val_min"] is not None]
        maxs = [float(r["val_max"]) for r in rows if r["val_max"] is not None]
        if agg == "sum":
            value = total_sum
        elif agg == "avg":
            value = total_sum / total_n if total_n else 0.0
        elif agg == "last":
            value = float(rows[-1]["val_sum"]) / float(rows[-1]["val_n"] or 1)
        elif agg == "max":
            value = max(maxs) if maxs else 0.0
        elif agg == "min":
            value = min(mins) if mins else 0.0
        else:  # pragma: no cover - defensive
            value = total_sum
        conn.execute(
            "INSERT INTO daily (metric, day, source, unit, agg, value, val_n, val_min, val_max)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(metric, day, source) DO UPDATE SET unit=excluded.unit, agg=excluded.agg,"
            " value=excluded.value, val_n=excluded.val_n, val_min=excluded.val_min, val_max=excluded.val_max",
            (
                metric,
                day,
                src,
                unit,
                agg,
                value,
                total_n,
                min(mins) if mins else None,
                max(maxs) if maxs else None,
            ),
        )


def write_sleep(conn: sqlite3.Connection, spans: Iterable[SleepSpan], *, source: str = "") -> WriteResult:
    """Store sleep intervals. Idempotent on ``(start, end, stage, source)``."""
    result = WriteResult()
    tz = config.tzinfo()
    rows = []
    for span in spans:
        start, end = int(span.start_ts), int(span.end_ts)
        if end < start:
            start, end = end, start
        duration = span.duration_s if span.duration_s is not None else float(end - start)
        if duration <= 0:
            result.skipped += 1
            continue
        src = span.source or source or config.default_source()
        day = day_of(end if end > start else start, None, tz)
        rows.append((start, end, span.stage, src, float(duration), day))
    if not rows:
        return result
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO sleep (start_ts, end_ts, stage, source, duration_s, day) VALUES (?,?,?,?,?,?)",
        rows,
    )
    result.inserted = conn.total_changes - before
    result.duplicates = len(rows) - result.inserted
    result.add("sleep", len(rows))
    conn.commit()
    return result


def write_workouts(
    conn: sqlite3.Connection,
    workouts: Iterable[Workout],
    *,
    source: str = "",
) -> WriteResult:
    """Store workouts. ``INSERT OR IGNORE`` - an id is stable once assigned."""
    result = WriteResult()
    tz = config.tzinfo()
    rows = []
    for w in workouts:
        src = w.source or source or config.default_source()
        start, end = int(w.start_ts), int(w.end_ts)
        if end < start:
            start, end = end, start
        duration = float(w.end_ts - w.start_ts) if w.end_ts > w.start_ts else 0.0
        wid = w.id or workout_id(w.activity, start, src)
        rows.append(
            (
                wid,
                w.activity,
                start,
                end,
                duration,
                w.energy_kcal,
                w.distance_m,
                w.avg_hr,
                w.max_hr,
                src,
                day_of(start, None, tz),
            )
        )
    if not rows:
        return result
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO workouts (id, activity, start_ts, end_ts, duration_s, energy_kcal, distance_m,"
        " avg_hr, max_hr, source, day) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    result.inserted = conn.total_changes - before
    result.duplicates = len(rows) - result.inserted
    result.add("workouts", len(rows))
    conn.commit()
    return result


def workout_id(activity: str, start_ts: int, source: str) -> str:
    import hashlib

    digest = hashlib.sha1(f"{source}|{activity}|{int(start_ts)}".encode("utf-8")).hexdigest()
    return digest[:16]


def insert_raw(conn: sqlite3.Connection, samples: Iterable[Sample], *, source: str = "") -> WriteResult:
    """Insert raw samples only, without touching rollups.

    The bulk path (a 360k-record export import): call this in batches, then
    :func:`rebuild_rollups` once at the end. A full-import rebuild is set-based,
    where the incremental path rebuilds one hour at a time.
    """
    result = WriteResult()
    tz = config.tzinfo()
    rows: List[Tuple[Any, ...]] = []
    for sample in samples:
        try:
            value = float(sample.value)
        except (TypeError, ValueError):
            result.skipped += 1
            continue
        if value != value:
            result.skipped += 1
            continue
        metric = sample.metric_obj()
        src = sample.source or source or config.default_source()
        day = day_of(sample.ts, sample.offset_s, tz)
        rows.append((metric.key, int(sample.ts), src, value, sample.unit or metric.unit, day, metric.agg))
        result.add(metric.key, 1)
    if not rows:
        return result
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO samples_raw (metric, ts, source, value, unit, day, agg)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    inserted = conn.total_changes - before
    result.inserted = inserted
    result.duplicates = len(rows) - inserted
    conn.commit()
    return result


def rebuild_rollups(conn: sqlite3.Connection) -> Dict[str, int]:
    """Rebuild every hourly bucket and daily row the raw table still covers.

    This is the one-shot path after a bulk import: it rebuilds from all raw
    rows present, however old. Hours whose raw rows were pruned are deliberately
    left untouched - they are already rolled up, and recomputing them would
    delete history. An empty raw table means the rollups are left exactly as
    they are.
    """
    bounds = conn.execute("SELECT MIN(ts) AS lo, MIN(day) AS day FROM samples_raw").fetchone()
    if not bounds or bounds["lo"] is None:
        return {
            "hourly": int(conn.execute("SELECT COUNT(*) AS n FROM samples_hourly").fetchone()["n"]),
            "daily": int(conn.execute("SELECT COUNT(*) AS n FROM daily").fetchone()["n"]),
            "note": "no raw samples to rebuild from",
        }
    conn.execute("DELETE FROM samples_hourly WHERE hour_ts >= ?", (bounds["lo"],))
    conn.execute(
        "INSERT INTO samples_hourly (metric, hour_ts, source, day, unit, agg, val_sum, val_n, val_min, val_max)"
        " SELECT metric, ts - (ts % 3600) AS bucket, source, MAX(day), MAX(unit), MAX(agg),"
        " SUM(value), COUNT(*), MIN(value), MAX(value)"
        " FROM samples_raw"
        " GROUP BY metric, bucket, source",
    )
    day_floor = bounds["day"]
    conn.execute("DELETE FROM daily WHERE day >= ?", (day_floor,))
    conn.execute(
        "INSERT INTO daily (metric, day, source, unit, agg, value, val_n, val_min, val_max)"
        " SELECT h.metric, h.day, h.source, MAX(h.unit), MAX(h.agg),"
        " CASE MAX(h.agg)"
        "   WHEN 'sum'  THEN SUM(h.val_sum)"
        "   WHEN 'avg'  THEN SUM(h.val_sum) / SUM(h.val_n)"
        "   WHEN 'last' THEN (SELECT h2.val_sum / h2.val_n FROM samples_hourly h2"
        "                     WHERE h2.metric = h.metric AND h2.day = h.day AND h2.source = h.source"
        "                     ORDER BY h2.hour_ts DESC LIMIT 1)"
        "   WHEN 'max'  THEN MAX(h.val_max)"
        "   WHEN 'min'  THEN MIN(h.val_min)"
        "   ELSE SUM(h.val_sum) END,"
        " SUM(h.val_n), MIN(h.val_min), MAX(h.val_max)"
        " FROM samples_hourly h WHERE h.day >= ?"
        " GROUP BY h.metric, h.day, h.source",
        (day_floor,),
    )
    conn.commit()
    return {
        "hourly": int(conn.execute("SELECT COUNT(*) AS n FROM samples_hourly").fetchone()["n"]),
        "daily": int(conn.execute("SELECT COUNT(*) AS n FROM daily").fetchone()["n"]),
    }


def log_ingest(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source: str,
    rows: int,
    metrics: Optional[Dict[str, Any]] = None,
    detail: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO ingest_log (at_ts, kind, source, rows, metrics, detail) VALUES (?,?,?,?,?,?)",
        (int(time.time()), kind, source, int(rows), json.dumps(metrics or {}), detail),
    )
    set_meta(conn, "last_ingest_at", ts_now_iso())
    conn.commit()


def prune_raw(conn: sqlite3.Connection, *, keep_days: int = DEFAULT_RAW_KEEP_DAYS) -> int:
    """Drop raw samples older than the window; rollups keep the history."""
    if keep_days <= 0:
        return 0
    cutoff = int(time.time()) - keep_days * 86400
    before = conn.total_changes
    conn.execute("DELETE FROM samples_raw WHERE ts < ?", (cutoff,))
    removed = conn.total_changes - before
    if removed:
        conn.commit()
    return removed


# ── reads used by more than one front end ───────────────────────────────────


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    out = {}
    for table in ("samples_raw", "samples_hourly", "daily", "sleep", "workouts", "ingest_log"):
        out[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    return out


def known_metrics(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT metric, SUM(val_n) AS n FROM samples_hourly GROUP BY metric ORDER BY n DESC"
    ).fetchall()
    return [r["metric"] for r in rows]


def sample_range(conn: sqlite3.Connection, metric: Optional[str] = None) -> Tuple[Optional[int], Optional[int]]:
    if metric:
        row = conn.execute(
            "SELECT MIN(hour_ts) AS a, MAX(hour_ts) AS b FROM samples_hourly WHERE metric=?", (metric,)
        ).fetchone()
    else:
        row = conn.execute("SELECT MIN(hour_ts) AS a, MAX(hour_ts) AS b FROM samples_hourly").fetchone()
    return (row["a"], row["b"]) if row else (None, None)


def last_ingest(conn: sqlite3.Connection, limit: int = 5) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT at_ts, kind, source, rows, metrics, detail FROM ingest_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "at": r["at_ts"],
                "at_iso": datetime.fromtimestamp(r["at_ts"], config.tzinfo()).isoformat(timespec="seconds"),
                "kind": r["kind"],
                "source": r["source"],
                "rows": r["rows"],
                "metrics": json.loads(r["metrics"] or "{}"),
                "detail": r["detail"],
            }
        )
    return out


def cursor(conn: sqlite3.Connection) -> Dict[str, Any]:
    """How far back the store already has data, per metric and globally.

    The Shortcuts recipe calls this first so each run sends a delta instead of a
    fixed window.
    """
    per_metric = {}
    for row in conn.execute(
        "SELECT metric, MAX(ts) AS last_ts, COUNT(*) AS n FROM samples_raw GROUP BY metric"
    ):
        per_metric[row["metric"]] = {"last_ts": row["last_ts"], "raw_rows": row["n"], "exact": True}
    # Raw rows carry the exact sample time; a pruned metric can only report its
    # last bucket start, which errs early (a re-sent hour is idempotent anyway).
    for row in conn.execute("SELECT metric, MAX(hour_ts) AS last_ts FROM samples_hourly GROUP BY metric"):
        entry = per_metric.setdefault(row["metric"], {"last_ts": None, "raw_rows": 0, "exact": False})
        if entry.get("exact"):
            continue
        if entry["last_ts"] is None or (row["last_ts"] or 0) > entry["last_ts"]:
            entry["last_ts"] = row["last_ts"]
    for entry in per_metric.values():
        entry.pop("exact", None)
    sleep_row = conn.execute("SELECT MAX(end_ts) AS last_ts FROM sleep").fetchone()
    workout_row = conn.execute("SELECT MAX(end_ts) AS last_ts FROM workouts").fetchone()
    return {
        "metrics": per_metric,
        "sleep_last_ts": sleep_row["last_ts"] if sleep_row else None,
        "workout_last_ts": workout_row["last_ts"] if workout_row else None,
        "raw_keep_days": DEFAULT_RAW_KEEP_DAYS,
    }
