"""What the plugin registers: seven tools, one slash command, one skill.

The tools read the SQLite store directly, in-process. That is deliberate: an
MCP hop would add a subprocess to every call and a second copy of the schema
logic, and the store is a local file. The bundled stdio MCP server exists for
harnesses that are not Hermes, not for this one.

Every tool returns a JSON string (what ``tools.registry`` expects) and every
tool degrades instead of raising: a missing store, an unknown metric, or a
locked database comes back as a readable message the model can act on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .hermes_health import __version__, config, queries, store  # noqa: F401
from .hermes_health.dashboard import render_dashboard
from .hermes_health.importer import import_export  # noqa: F401  (re-exported for callers)
from .hermes_health.server import serve as serve_ingest  # noqa: F401

TOOLSET = "health"
SKILL_NAME = "health-brief"

_FIELD = lambda kind, description, **extra: {"type": kind, "description": description, **extra}  # noqa: E731


def _schema(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


SCHEMAS: List[Dict[str, Any]] = [
    _schema(
        "health_summary",
        "Apple Health, everything at once: today's activity rings and steps, resting heart rate, HRV, "
        "last night's sleep with stages, recent workouts, and which numbers moved versus the previous "
        "window. Use this first for any 'how am I doing' question, then drill in with health_query or "
        "health_sleep for specifics.",
        {
            "days": _FIELD("integer", "Window in days for the activity/heart blocks (default 7)."),
            "end": _FIELD("string", "Last day of the window as YYYY-MM-DD (default today)."),
        },
        [],
    ),
    _schema(
        "health_query",
        "Daily values for one health metric, with average, latest, min/max and trend. Metric names are "
        "flexible: 'steps', 'Steps', 'step_count' and 'HKQuantityTypeIdentifierStepCount' all resolve to "
        "the same series, so Health Auto Export and Shortcuts spellings both work.",
        {
            "metric": _FIELD(
                "string",
                "Metric name or alias: steps, resting_heart_rate, hrv, active_energy, sleep, vo2_max, weight...",
            ),
            "days": _FIELD("integer", "Window in days (default 30)."),
            "start": _FIELD("string", "Start day as YYYY-MM-DD."),
            "end": _FIELD("string", "End day as YYYY-MM-DD."),
            "source": _FIELD("string", "Only this device's samples (e.g. 'Tyler's Apple Watch')."),
        },
        ["metric"],
    ),
    _schema(
        "health_sleep",
        "Sleep clustered into nights, attributed to the wake-up day: bedtime, wake time, time asleep, "
        "stage breakdown (core/deep/REM/awake), efficiency, sleep interruptions, 7-night averages, "
        "bedtime/wake consistency, and naps flagged separately.",
        {
            "days": _FIELD("integer", "Window in days (default 14)."),
            "end": _FIELD("string", "Last day of the window as YYYY-MM-DD."),
        },
        [],
    ),
    _schema(
        "health_workouts",
        "Workouts with duration, active energy, distance and heart rate, grouped by activity, plus rest "
        "days. Note: iOS Shortcuts cannot read workouts, so these only exist if the Health export was "
        "imported or Health Auto Export is sending them.",
        {
            "days": _FIELD("integer", "Window in days (default 30)."),
            "activity": _FIELD("string", "Filter to one activity: running, walking, cycling, hiit, ..."),
            "end": _FIELD("string", "Last day of the window as YYYY-MM-DD."),
        },
        [],
    ),
    _schema(
        "health_metrics",
        "Which metrics this store actually holds data for, with units and how each is aggregated on the "
        "way in. Call this before guessing a metric name.",
        {"search": _FIELD("string", "Substring filter, e.g. 'heart' or 'sleep'.")},
        [],
    ),
    _schema(
        "health_status",
        "Store health: where the database is, row counts, data range, the last delivery from the phone, "
        "and hours since the newest sample. Check this first when a health number looks stale or empty.",
        {},
        [],
    ),
    _schema(
        "health_dashboard",
        "Render the health dashboard to an HTML file (rings, resting-HR and HRV trends, sleep stages, "
        "recent workouts) and return its path. After calling this, show it to the user by emitting "
        "::preview{file=\"<path>\"} on its own line.",
        {
            "days": _FIELD("integer", "Activity window in days (default 7)."),
            "metric_days": _FIELD("integer", "Trend window in days for the charts (default 14)."),
            "end": _FIELD("string", "Last day of the window as YYYY-MM-DD."),
            "open": _FIELD("boolean", "Also open the file in the default browser.", default=False),
        },
        [],
    ),
]


class Settings:
    """Resolved plugin settings: config first, environment second, defaults last."""

    def __init__(self, ctx=None) -> None:
        self.ctx = ctx

    def get(self, key: str, default: Any = None) -> Any:
        value = None
        if self.ctx is not None:
            try:
                value = self.ctx.get_config(key, None)
            except Exception:  # a settings read must never break a tool call
                value = None
        if value in (None, "", []):
            return default
        return value

    @property
    def db_path(self) -> str:
        configured = self.get("db_path", "")
        if configured:
            return str(Path(str(configured)).expanduser())
        return str(config.default_db_path())

    @property
    def timezone(self) -> Optional[str]:
        return self.get("timezone", "") or None

    @property
    def raw_keep_days(self) -> int:
        try:
            return int(self.get("raw_keep_days", store.DEFAULT_RAW_KEEP_DAYS))
        except (TypeError, ValueError):
            return store.DEFAULT_RAW_KEEP_DAYS

    @property
    def widget_dir(self) -> Path:
        configured = self.get("widget_dir", "")
        if configured:
            return Path(str(configured)).expanduser()
        return Path(self.db_path).parent


def _open_store(settings: Settings):
    """A connection plus a hint when the store is empty or missing."""
    return store.connect(settings.db_path)


def _ensure_timezone(settings: Settings) -> None:
    """Apply the plugin's configured timezone for this call.

    Assignment, not ``setdefault``: an explicit plugin setting is the operator's
    answer, so it wins over an inherited environment value.
    """
    if settings.timezone:
        os.environ["HERMES_HEALTH_TZ"] = str(settings.timezone)


def _fail(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra})


def _empty_store_hint(path: str) -> str:
    return (
        f"No Apple Health data is stored at {path} yet. Two ways to fill it: "
        "import a Health export with `hermes-health import-export <export.xml>`, or start the ingest "
        "server (`hermes-health serve`) and push from the phone with an iOS Shortcut or a Health Auto "
        "Export REST automation. Check `hermes-health status` for what is in the store."
    )


def make_handler(name: str, settings: Settings) -> Callable[..., str]:
    """Build the ``handler(args, **kwargs) -> str`` that Hermes' registry expects."""

    def handler(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        args = args or {}
        _ensure_timezone(settings)
        try:
            conn = _open_store(settings)
        except Exception as exc:
            return _fail(f"cannot open the health store at {settings.db_path}: {exc}")
        try:
            if name == "health_summary":
                payload = queries.summary(
                    conn, days=_int(args.get("days"), 7), end_day=args.get("end") or None
                )
                if not payload.get("data", {}).get("newest_data_ts"):
                    return _fail(_empty_store_hint(settings.db_path))
            elif name == "health_query":
                metric = args.get("metric")
                if not metric:
                    return _fail("metric is required")
                payload = queries.query(
                    conn,
                    str(metric),
                    days=_int(args.get("days"), 30),
                    start_day=args.get("start") or None,
                    end_day=args.get("end") or None,
                    source=args.get("source") or None,
                )
                if not payload["series"]:
                    known = store.known_metrics(conn)
                    return _fail(
                        f"no stored data for {payload['metric']} in that window",
                        metric=payload["metric"],
                        metrics_with_data=known[:40],
                    )
            elif name == "health_sleep":
                payload = queries.sleep_report(
                    conn, days=_int(args.get("days"), 14), end_day=args.get("end") or None
                )
                if not payload["nights"]:
                    return _fail(
                        "no sleep recorded in that window",
                        note="Shortcuts can read sleep; if it is missing, the phone has not pushed it yet.",
                    )
            elif name == "health_workouts":
                payload = queries.workout_report(
                    conn, days=_int(args.get("days"), 30), end_day=args.get("end") or None
                )
                activity = args.get("activity")
                if activity:
                    slugged = str(activity).strip().lower().replace(" ", "_")
                    payload["sessions"] = [
                        session for session in payload["sessions"] if session["activity"] == slugged
                    ]
                    payload["n"] = len(payload["sessions"])
            elif name == "health_metrics":
                search = args.get("search")
                if search:
                    found = queries.search_metrics(conn, str(search))
                    payload = {"search": str(search), "count": len(found), "metrics": found}
                else:
                    payload = queries.catalog(conn)
            elif name == "health_status":
                payload = queries.status(conn)
            elif name == "health_dashboard":
                payload = _dashboard(conn, settings, args)
            else:
                return _fail(f"unknown tool {name}")
        except Exception as exc:
            return _fail(f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()
        return json.dumps({"ok": True, **payload}, default=str)

    return handler


def _dashboard(conn, settings: Settings, args: Dict[str, Any]) -> Dict[str, Any]:
    from .hermes_health.dashboard import render_dashboard

    days = _int(args.get("days"), 7)
    metric_days = _int(args.get("metric_days"), 14)
    html = render_dashboard(
        conn, days=days, metric_days=metric_days, end_day=args.get("end") or None
    )
    directory = settings.widget_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"health-widget-{stamp}.html"
    path.write_text(html, encoding="utf-8")
    if args.get("open"):
        import webbrowser

        webbrowser.open(path.as_uri())
    return {
        "widget_path": str(path),
        "days": days,
        "metric_days": metric_days,
        "instructions": f'Show it with a line that reads exactly: ::preview{{file="{path}"}}',
    }


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _command_handler(settings: Settings, raw_args: str = "") -> str:
    """``/health`` — a one-line status or the brief, straight into the chat."""
    argv = (raw_args or "").split()
    _ensure_timezone(settings)
    try:
        conn = _open_store(settings)
    except Exception as exc:
        return f"health store unavailable: {exc}"
    try:
        if argv and argv[0] in ("brief", "summary"):
            days = _int(argv[1], 7) if len(argv) > 1 else 7
            from .hermes_health.cli import _brief

            return _brief(conn, days)
        status = queries.status(conn)
        fresh = status["freshness"]
        counts_row = status["counts"]
        lines = [
            f"hermes-health {status['version']} — {status['db_path']}",
            f"{status['metric_count']} metrics · raw {counts_row['samples_raw']:,} ·"
            f" hourly {counts_row['samples_hourly']:,} · daily {counts_row['daily']:,} ·"
            f" sleep {counts_row['sleep']:,} · workouts {counts_row['workouts']:,}",
        ]
        if fresh.get("newest_data_iso"):
            lines.append(f"newest data {fresh['newest_data_iso']} ({fresh.get('hours_since_data')}h ago)")
        else:
            lines.append("no data yet — import an export or push from the phone")
        lines.append("usage: /health [brief [days]] · tools: health_summary, health_query, health_sleep")
        return "\n".join(lines)
    finally:
        conn.close()


def register_all(ctx) -> None:
    """Register tools, the slash command, and the bundled skill."""
    settings = Settings(ctx)
    for schema in SCHEMAS:
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=make_handler(schema["name"], settings),
            description=schema["description"].split(".")[0],
        )
    if callable(getattr(ctx, "register_command", None)):
        ctx.register_command(
            "health",
            lambda raw: _command_handler(settings, raw),
            description="Apple Health status, or the plain-text brief",
            args_hint="[brief [days]]",
        )
    if callable(getattr(ctx, "register_skill", None)):
        skill_path = Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"
        if skill_path.exists():
            ctx.register_skill(SKILL_NAME, skill_path)
