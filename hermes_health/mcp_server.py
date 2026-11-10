"""Stdio MCP server: the same store, reachable from any MCP harness.

Zero non-standard-library dependencies, newline-delimited JSON-RPC on
stdin/stdout, which is what MCP's stdio transport is. Hermes does not need this
(it loads the plugin's tools in-process), but Claude Code, Cursor, and anything
else that speaks MCP get the same six tools from the same SQLite file.

    {"mcpServers": {"health": {"command": "python3",
                               "args": ["/path/to/hermes-health/health_mcp.py"]}}}
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Callable, Dict, List, Optional

from . import __version__, config, queries, store

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "hermes-health"
MAX_TEXT_CHARS = 60_000

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "health_summary",
        "description": (
            "Everything at once for a health brief: activity rings, steps, resting heart rate, HRV, "
            "last night's sleep, recent workouts, and which numbers moved versus the previous window. "
            "Start here, then drill in with health_query or health_sleep."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days (default 7)."},
                "end": {"type": "string", "description": "Last day of the window, YYYY-MM-DD."},
            },
        },
    },
    {
        "name": "health_query",
        "description": (
            "Daily values for one health metric over time, plus average, latest, and trend. "
            "Metric names are flexible: 'steps', 'Steps', 'step_count' or "
            "'HKQuantityTypeIdentifierStepCount' all resolve to the same series."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "Metric name or alias."},
                "days": {"type": "integer", "description": "Window in days (default 30)."},
                "start": {"type": "string", "description": "Start day, YYYY-MM-DD."},
                "end": {"type": "string", "description": "End day, YYYY-MM-DD."},
                "source": {"type": "string", "description": "Only this device's data (optional)."},
            },
            "required": ["metric"],
        },
    },
    {
        "name": "health_sleep",
        "description": (
            "Sleep sessions clustered into nights, with stages (core/deep/REM/awake), bedtime, wake "
            "time, efficiency, 7-night averages, and consistency. Naps are flagged, not dropped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days (default 14)."},
                "end": {"type": "string", "description": "Last day of the window, YYYY-MM-DD."},
            },
        },
    },
    {
        "name": "health_workouts",
        "description": (
            "Workouts with duration, energy, distance, and heart rate, grouped by activity. "
            "Note: Shortcuts cannot read workouts, so these come from the Health export or a "
            "Health Auto Export automation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days (default 30)."},
                "activity": {"type": "string", "description": "Filter: running, walking, cycling, ..."},
                "end": {"type": "string", "description": "Last day of the window, YYYY-MM-DD."},
            },
        },
    },
    {
        "name": "health_metrics",
        "description": "Which metrics this store holds, with units and how each is aggregated. Filter with 'search'.",
        "inputSchema": {
            "type": "object",
            "properties": {"search": {"type": "string", "description": "Substring filter, e.g. 'heart'."}},
        },
    },
    {
        "name": "health_status",
        "description": (
            "Store health: row counts, data range, last delivery from the phone, and how many hours "
            "since the newest sample. Check this first when a number looks stale."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class HealthMCP:
    """Protocol handling, separated from stdio so it can be driven in tests."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = str(config.db_path(db_path))

    def _conn(self) -> sqlite3.Connection:
        return store.connect(self.db_path)

    # ── tools ───────────────────────────────────────────────────────────────
    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = arguments or {}
        conn = self._conn()
        try:
            if name == "health_summary":
                payload = queries.summary(
                    conn, days=_int(args.get("days"), 7), end_day=args.get("end") or None
                )
            elif name == "health_query":
                metric = args.get("metric")
                if not metric:
                    return _error("metric is required")
                payload = queries.query(
                    conn,
                    str(metric),
                    days=_int(args.get("days"), 30),
                    start_day=args.get("start") or None,
                    end_day=args.get("end") or None,
                    source=args.get("source") or None,
                )
            elif name == "health_sleep":
                payload = queries.sleep_report(
                    conn, days=_int(args.get("days"), 14), end_day=args.get("end") or None
                )
            elif name == "health_workouts":
                payload = queries.workout_report(
                    conn,
                    days=_int(args.get("days"), 30),
                    end_day=args.get("end") or None,
                )
                activity = args.get("activity")
                if activity:
                    slugged = str(activity).lower().replace(" ", "_")
                    payload["sessions"] = [
                        s for s in payload["sessions"] if s["activity"] == slugged
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
            else:
                return _error(f"unknown tool {name}")
        except Exception as exc:  # a tool failure must come back as a tool error, not a crash
            return _error(f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()
        return {
            "content": [{"type": "text", "text": _dump(payload)}],
            "structuredContent": payload if _small(payload) else None,
            "isError": False,
        }

    # ── protocol ────────────────────────────────────────────────────────────
    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """One JSON-RPC message in, one response out (``None`` for notifications)."""
        if not isinstance(request, dict):
            return _rpc_error(None, -32600, "invalid request")
        method = request.get("method")
        request_id = request.get("id")
        if method is None:
            # A response to something we sent; nothing to answer.
            return None
        if request_id is None and method.startswith("notifications/"):
            return None
        if method == "initialize":
            client_protocol = (request.get("params") or {}).get("protocolVersion")
            return _ok(
                request_id,
                {
                    "protocolVersion": client_protocol
                    if client_protocol in SUPPORTED_PROTOCOLS
                    else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                    "instructions": (
                        "Apple Health data pushed from a phone. health_summary gives activity, sleep, "
                        "heart, and workouts together; health_query reads one metric as a daily series."
                    ),
                },
            )
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return _ok(request_id, {})
        if method == "tools/list":
            return _ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            if not name:
                return _rpc_error(request_id, -32602, "params.name is required")
            result = self.call_tool(str(name), params.get("arguments"))
            return _ok(request_id, result)
        if method in ("resources/list",):
            return _ok(request_id, {"resources": []})
        if method in ("prompts/list",):
            return _ok(request_id, {"prompts": []})
        if method == "shutdown":
            return _ok(request_id, {})
        return _rpc_error(request_id, -32601, f"method not found: {method}")

    def serve(self, stdin=None, stdout=None) -> None:
        """Read newline-delimited JSON-RPC until stdin closes."""
        source = stdin or sys.stdin
        sink = stdout or sys.stdout
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                _write(sink, _rpc_error(None, -32700, "parse error"))
                continue
            response = self.handle(request)
            if response is not None:
                _write(sink, response)


def _write(sink, payload: Dict[str, Any]) -> None:
    sink.write(json.dumps(payload, default=str) + "\n")
    sink.flush()


def _ok(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(message: str) -> Dict[str, Any]:
    """A tool-level failure: a successful call whose result reports an error."""
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def _rpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    """A protocol-level failure: JSON-RPC's own ``error`` member."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dump(payload: Dict[str, Any]) -> str:
    text = json.dumps(payload, indent=2, default=str)
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + "\n... truncated; narrow the window with days/start/end"


def _small(payload: Dict[str, Any]) -> bool:
    return len(json.dumps(payload, default=str)) <= MAX_TEXT_CHARS


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    db: Optional[str] = None
    if "--db" in argv:
        db = argv[argv.index("--db") + 1]
    if "--version" in argv:
        print(f"{SERVER_NAME} {__version__}")
        return 0
    if "--selftest" in argv:
        server = HealthMCP(db)
        print(json.dumps(server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), indent=2)[:800])
        print(json.dumps(server.call_tool("health_status"), default=str)[:1200])
        return 0
    HealthMCP(db).serve()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
