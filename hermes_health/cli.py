"""Command line: the same store from a shell, a launchd job, or a cron job.

    hermes-health serve                     # the ingest endpoint (the always-on job)
    hermes-health import-export export.xml  # backfill from a Health export
    hermes-health status                    # is data arriving, and how fresh is it
    hermes-health summary --days 7          # what a brief quotes
    hermes-health query resting_heart_rate --days 30
    hermes-health widget --out /tmp/health.html
    hermes-health mcp                       # stdio MCP server for other harnesses
    hermes-health mcp-config                # config block to paste into any harness
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import __version__, config, queries, store


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        text = f"{value:,.1f}" if abs(value) < 1000 else f"{value:,.0f}"
    else:
        text = f"{value:,}"
    return f"{text}{unit}"


def _brief(conn, days: int = 7, end: Optional[str] = None) -> str:
    """Deterministic plain-text brief: no model involved, so it is also a fallback."""
    summary = queries.summary(conn, days=days, end_day=end)
    today = summary["today"]
    rings = today["rings"]
    sleep = summary["sleep"]
    last = sleep.get("last_night") or {}
    averages = sleep.get("averages") or {}
    workouts = summary["workouts"]
    lines: List[str] = []
    stamp = summary["generated_at"][:16].replace("T", " ")
    lines.append(f"HEALTH BRIEF — {today['day']} ({stamp})")
    freshness = summary["data"]
    if freshness.get("hours_since_data") is not None:
        lines.append(f"data through {freshness.get('newest_data_iso', '')[0:16].replace('T', ' ')}"
                     f" ({freshness['hours_since_data']}h ago)")
    lines.append("")
    lines.append("TODAY")
    for key, label in (("active_energy", "Move"), ("exercise_minutes", "Exercise"), ("stand_hours", "Stand")):
        ring = rings[key]
        lines.append(f"  {label:9s} {_fmt(ring['value'])} / {_fmt(ring['goal'])} {ring['unit']}"
                     f"  ({ring['pct']:.0f}%)")
    lines.append(f"  steps     {_fmt(today.get('steps_today'))}")
    lines.append("")
    if last:
        lines.append("LAST NIGHT")
        lines.append(f"  asleep {last.get('asleep')}  ({last.get('bedtime')} → {last.get('wake')},"
                     f" {last.get('efficiency_pct')}% efficiency)")
        lines.append(f"  deep {last.get('deep_hours')}h · REM {last.get('rem_hours')}h ·"
                     f" core {last.get('core_hours')}h · awake {last.get('awake_hours')}h")
        lines.append(f"  7-night average {averages.get('asleep')} asleep, bedtime {averages.get('bedtime')},"
                     f" wake {averages.get('wake')}")
        previous = sleep.get("previous_average_asleep_hours")
        if previous is not None and averages.get("asleep_hours") is not None:
            delta = averages["asleep_hours"] - previous
            lines.append(f"  vs previous 7 nights: {delta:+.2f}h")
        if last.get("is_nap"):
            lines.append("  note: this was a nap, not a full night")
    else:
        lines.append("LAST NIGHT: no sleep recorded")
    lines.append("")
    lines.append("HEART")
    for key, label in (
        ("resting_heart_rate", "resting HR"),
        ("resting_heart_rate_28d", "resting HR 28d"),
        ("hrv", "HRV"),
        ("hrv_28d", "HRV 28d"),
    ):
        block = summary["heart"][key]
        trend = block.get("trend") or {}
        lines.append(f"  {label:15s} avg {_fmt(block.get('average'))} {block.get('unit') or ''}"
                     f"  latest {_fmt(block.get('latest'))}  ({trend.get('direction', 'flat')})")
    lines.append("")
    lines.append(f"WORKOUTS (last {workouts.get('days')} days)")
    if workouts.get("n"):
        lines.append(f"  {workouts['n']} sessions · {_fmt(workouts.get('total_minutes'))} min ·"
                     f" {_fmt(workouts.get('total_kcal'))} Cal · {workouts.get('rest_days')} rest days")
        for bucket in workouts.get("by_activity", [])[:5]:
            lines.append(f"    {bucket['activity'].replace('_', ' '):14s} {bucket['sessions']}x"
                         f"  {_fmt(bucket['minutes'])} min")
    else:
        lines.append(f"  none. {workouts.get('source_note') or ''}")
    lines.append("")
    lines.append(f"CHANGES (vs previous {days} days)")
    for metric, change in summary["changes"].items():
        lines.append(f"  {metric.replace('_', ' '):22s} {change['recent_average']} vs"
                     f" {change['previous_average']}  ({change['change_pct']:+}% {change['direction']})"
                     if change["change_pct"] is not None
                     else f"  {metric.replace('_', ' '):22s} not enough data")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-health", description="Apple Health data for AI agents")
    parser.add_argument("--version", action="version", version=f"hermes-health {__version__}")
    parser.add_argument("--db", help="SQLite path (default $HERMES_HEALTH_DB or ~/.hermes-health/health.db)")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the ingest HTTP server")
    p_serve.add_argument("--host", help="bind address (default $HERMES_HEALTH_HOST or 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="port (default $HERMES_HEALTH_PORT or 8787)")
    p_serve.add_argument("--token", help="bearer token guests must send (default $HERMES_HEALTH_TOKEN)")
    p_serve.add_argument("--quiet", action="store_true", help="log requests only when they carry data")
    p_serve.add_argument("--verbose", action="store_true", help="log every request")

    p_imp = sub.add_parser("import-export", help="import an Apple Health export.xml")
    p_imp.add_argument("path", help="path to export.xml")
    p_imp.add_argument("--since", help="only records at/after this time")
    p_imp.add_argument("--until", help="only records at/before this time")
    p_imp.add_argument("--types", help="comma-separated metric names to keep")
    p_imp.add_argument("--no-rebuild", action="store_true", help="skip the rollup rebuild (not recommended)")
    p_imp.add_argument("--prune", action="store_true", help="prune raw samples older than the window afterwards")

    p_sum = sub.add_parser("summary", help="activity, sleep, heart, workouts in one payload")
    p_sum.add_argument("--days", type=int, default=7)
    p_sum.add_argument("--end", help="last day of the window, YYYY-MM-DD")
    p_sum.add_argument("--json", action="store_true", help="machine-readable output")

    p_query = sub.add_parser("query", help="one metric as a daily series")
    p_query.add_argument("metric")
    p_query.add_argument("--days", type=int, default=30)
    p_query.add_argument("--start")
    p_query.add_argument("--end")
    p_query.add_argument("--source")
    p_query.add_argument("--json", action="store_true")

    p_sleep = sub.add_parser("sleep", help="nights, stages, averages")
    p_sleep.add_argument("--days", type=int, default=14)
    p_sleep.add_argument("--end")
    p_sleep.add_argument("--json", action="store_true")

    p_wk = sub.add_parser("workouts", help="workouts grouped by activity")
    p_wk.add_argument("--days", type=int, default=30)
    p_wk.add_argument("--end")
    p_wk.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="what is in the store and how fresh it is")
    p_status.add_argument("--json", action="store_true")

    p_widget = sub.add_parser("widget", help="render the dashboard HTML")
    p_widget.add_argument("--days", type=int, default=7)
    p_widget.add_argument("--metric-days", type=int, default=14)
    p_widget.add_argument("--end")
    p_widget.add_argument("--out", help="output path (default: a fresh file under ~/.hermes-health/)")

    p_brief = sub.add_parser("brief", help="plain-text brief, no model call")
    p_brief.add_argument("--days", type=int, default=7)
    p_brief.add_argument("--end")

    sub.add_parser("mcp", help="run the stdio MCP server")

    p_cfg = sub.add_parser("mcp-config", help="print an MCP config block for another harness")
    p_cfg.add_argument("--python", default=sys.executable or "python3")

    sub.add_parser("rebuild", help="rebuild hourly and daily rollups from raw samples")

    p_prune = sub.add_parser("prune", help="drop raw samples older than the retention window")
    p_prune.add_argument("--keep-days", type=int, default=store.DEFAULT_RAW_KEEP_DAYS)
    p_prune.add_argument("--yes", action="store_true", help="required: this deletes rows")

    args = parser.parse_args(argv)
    command = args.command or "status"

    if command == "serve":
        from .server import serve

        serve(
            args.db,
            host=args.host,
            port=args.port,
            token=args.token,
            verbose=args.verbose and not args.quiet,
            banner=True,
        )
        return 0

    if command == "mcp":
        from .mcp_server import HealthMCP

        HealthMCP(args.db).serve()
        return 0

    if command == "mcp-config":
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "health_mcp.py")
        block = {
            "mcpServers": {
                "health": {
                    "command": args.python,
                    "args": [script],
                    "env": {"HERMES_HEALTH_DB": str(config.db_path(args.db))},
                }
            }
        }
        print(json.dumps(block, indent=2))
        return 0

    if command == "import-export":
        from .importer import import_export

        conn = store.connect(config.db_path(args.db))
        types = [t.strip() for t in args.types.split(",")] if getattr(args, "types", None) else None
        report = import_export(
            conn,
            args.path,
            since=args.since,
            until=args.until,
            types=types,
            progress=lambda message: print(message, flush=True),
        )
        for line in report.lines():
            print("  " + line)
        if not args.no_rebuild:
            print("  rollups:", store.rebuild_rollups(conn))
        if args.prune:
            print("  pruned raw rows:", store.prune_raw(conn))
        conn.close()
        return 0

    conn = store.connect(config.db_path(args.db))
    try:
        if command == "status":
            payload = queries.status(conn)
            if args.json:
                _print_json(payload)
            else:
                print(f"hermes-health {payload['version']}")
                print(f"  store       {payload['db_path']} ({_fmt((payload.get('db_bytes') or 0) / 1048576)} MB)")
                print(f"  timezone    {payload['timezone']}")
                counts_row = payload["counts"]
                print(f"  rows        raw {counts_row['samples_raw']:,} · hourly {counts_row['samples_hourly']:,}"
                      f" · daily {counts_row['daily']:,} · sleep {counts_row['sleep']:,}"
                      f" · workouts {counts_row['workouts']:,}")
                print(f"  metrics     {payload['metric_count']} with data")
                fresh = payload["freshness"]
                print(f"  newest data {fresh.get('newest_data_iso')} ({fresh.get('hours_since_data')}h ago)")
                delivery = fresh.get("last_delivery")
                if delivery:
                    print(f"  last push   {delivery['at_iso']} · {delivery['rows']} rows"
                          f" · {delivery['kind']} · {delivery['source']}")
                else:
                    print("  last push   none recorded")
            return 0
        if command == "summary":
            payload = queries.summary(conn, days=args.days, end_day=args.end)
            _print_json(payload) if args.json else print(_brief(conn, args.days, args.end))
            return 0
        if command == "brief":
            print(_brief(conn, args.days, args.end))
            return 0
        if command == "query":
            payload = queries.query(
                conn,
                args.metric,
                days=args.days,
                start_day=args.start,
                end_day=args.end,
                source=args.source,
            )
            if args.json:
                _print_json(payload)
            else:
                print(f"{payload['label']} ({payload['metric']}, {payload['unit']}, {payload['aggregation']})")
                print(f"  {payload['n']} days · average {_fmt(payload['average'])} ·"
                      f" trend {payload['trend']['direction']} ({payload['trend']['change_pct']}%)")
                for entry in payload["series"]:
                    bar = "#" * min(40, int((entry["value"] or 0) / max(1e-9, max(
                        (e["value"] or 0) for e in payload["series"])) * 40))
                    print(f"  {entry['day']}  {_fmt(entry['value']):>12}  {bar}")
            return 0
        if command == "sleep":
            payload = queries.sleep_report(conn, days=args.days, end_day=args.end)
            if args.json:
                _print_json(payload)
            else:
                for night in payload["nights"]:
                    print(f"  {night['day']} {night['weekday']}  {night['bedtime']} → {night['wake']}"
                          f"  asleep {night['asleep']}  deep {night['deep_hours']}h  rem {night['rem_hours']}h"
                          f"  eff {night['efficiency_pct']}%{ '  (nap)' if night['is_nap'] else ''}")
                print("  averages:", json.dumps(payload["averages"]))
            return 0
        if command == "workouts":
            payload = queries.workout_report(conn, days=args.days, end_day=args.end)
            if args.json:
                _print_json(payload)
            else:
                print(f"  {payload['n']} sessions in {payload['days']} days ·"
                      f" {_fmt(payload.get('total_minutes'))} min · {_fmt(payload.get('total_kcal'))} Cal")
                for session in payload["sessions"]:
                    extra = f" {session['distance_mi']} mi" if session.get("distance_mi") else ""
                    print(f"  {session['day']} {session['start']} {session['activity']:16s}"
                          f" {_fmt(session['duration_minutes'])} min{extra}"
                          f" {_fmt(session['energy_kcal'])} Cal")
            return 0
        if command == "widget":
            from .dashboard import render_dashboard

            html = render_dashboard(conn, days=args.days, metric_days=args.metric_days, end_day=args.end)
            out = args.out
            if not out:
                directory = config.default_db_path().parent
                directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                out = str(directory / f"widget-{stamp}.html")
            out = os.path.expanduser(out)
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(html)
            print(out)
            return 0
        if command == "rebuild":
            print(json.dumps(store.rebuild_rollups(conn)))
            return 0
        if command == "prune":
            if not args.yes:
                print("refusing to delete rows without --yes", file=sys.stderr)
                return 2
            print(json.dumps({"removed": store.prune_raw(conn, keep_days=args.keep_days)}))
            return 0
    finally:
        conn.close()
    parser.print_help()
    return 1
