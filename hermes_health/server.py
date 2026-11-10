"""The ingest endpoint: what the phone posts to.

Standard library only — ``http.server`` on the always-on host, so the receiver
has no install step. The same process serves the read endpoints, so a dashboard
or a phone can also ask what has landed.

Security posture: bind it to the private network (a mesh VPN or the LAN) and let
that be the auth layer. A bearer token is available for anything reachable some
other way, and its absence is *warned about* when the listener is not loopback —
loudly, once, at startup.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__, config
from . import queries, store
from .dashboard import render_dashboard
from .payloads import parse_json_text
from .store import DEFAULT_RAW_KEEP_DAYS

MAX_BODY_BYTES = 32 * 1024 * 1024
READ_PATHS = {"/health", "/", "/cursor", "/metrics", "/summary", "/query", "/sleep", "/workouts", "/dashboard", "/status"}


class Store:
    """Opens a connection per request: SQLite handles the concurrency, WAL the writers."""

    def __init__(self, db_path: str, raw_keep_days: int = DEFAULT_RAW_KEEP_DAYS) -> None:
        self.db_path = db_path
        self.raw_keep_days = raw_keep_days
        self._prune_lock = threading.Lock()
        self._last_prune = 0.0

    def connect(self):
        return store.connect(self.db_path)

    def maybe_prune(self, conn) -> None:
        """Prune raw samples at most once an hour, on the write path."""
        now = time.time()
        if now - self._last_prune < 3600:
            return
        with self._prune_lock:
            if now - self._last_prune < 3600:
                return
            self._last_prune = now
        try:
            removed = store.prune_raw(conn, keep_days=self.raw_keep_days)
            if removed:
                print(f"[hermes-health] pruned {removed} raw samples older than {self.raw_keep_days}d", flush=True)
        except Exception as exc:  # pragma: no cover - pruning must never break ingest
            print(f"[hermes-health] prune skipped: {exc}", flush=True)


def _json_bytes(payload: Any, status: int = 200) -> Tuple[int, bytes, str]:
    body = json.dumps(payload, default=str).encode("utf-8")
    return status, body, "application/json"


def make_handler(state: Store, token: str, verbose: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"hermes-health/{__version__}"
        protocol_version = "HTTP/1.1"

        # ── plumbing ────────────────────────────────────────────────────────
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
            if verbose:
                print(f"[hermes-health] {self.address_string()} {fmt % args}", flush=True)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            code, body, ctype = _json_bytes(payload, status)
            self._send(code, body, ctype)

        def _authorized(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            candidate = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
            if not candidate:
                candidate = self.headers.get("X-Health-Token", "").strip()
            if candidate == token:
                return True
            self._json(401, {"ok": False, "error": "unauthorized"})
            return False

        def _params(self) -> Dict[str, str]:
            parsed = urlparse(self.path)
            return {k: v[0] for k, v in parse_qs(parsed.query).items() if v}

        def _int_param(self, params: Dict[str, str], key: str, default: int) -> int:
            try:
                return int(params.get(key, default))
            except (TypeError, ValueError):
                return default

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return b""
            if length > MAX_BODY_BYTES:
                raise ValueError(f"body too large ({length} bytes)")
            chunks = []
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        # ── routes ──────────────────────────────────────────────────────────
        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path not in READ_PATHS:
                self._json(404, {"ok": False, "error": f"unknown path {path}"})
                return
            if not self._authorized():
                return
            params = self._params()
            conn = state.connect()
            try:
                if path in ("/health", "/", "/status"):
                    payload = queries.status(conn)
                    payload["ok"] = True
                    payload["endpoints"] = sorted(READ_PATHS | {"/ingest"})
                    payload["raw_keep_days"] = state.raw_keep_days
                    self._json(200, payload)
                    return
                if path == "/cursor":
                    payload = store.cursor(conn)
                    payload["ok"] = True
                    payload["server_time"] = int(time.time())
                    wanted = params.get("metrics")
                    if wanted:
                        keep = {name.strip() for name in wanted.split(",") if name.strip()}
                        payload["metrics"] = {
                            k: v
                            for k, v in payload["metrics"].items()
                            if k in keep or store.resolve(k).key in keep
                        }
                    self._json(200, payload)
                    return
                if path == "/metrics":
                    self._json(200, queries.catalog(conn))
                    return
                if path == "/summary":
                    self._json(200, queries.summary(conn, days=self._int_param(params, "days", 7)))
                    return
                if path == "/query":
                    metric = params.get("metric") or params.get("metrics") or ""
                    if not metric:
                        self._json(400, {"ok": False, "error": "metric is required"})
                        return
                    self._json(
                        200,
                        queries.query(
                            conn,
                            metric,
                            days=self._int_param(params, "days", 30),
                            start_day=params.get("start") or params.get("start_day"),
                            end_day=params.get("end") or params.get("end_day"),
                            source=params.get("source"),
                        ),
                    )
                    return
                if path == "/sleep":
                    self._json(200, queries.sleep_report(conn, days=self._int_param(params, "days", 14)))
                    return
                if path == "/workouts":
                    self._json(
                        200,
                        queries.workout_report(
                            conn,
                            days=self._int_param(params, "days", 30),
                        ),
                    )
                    return
                if path == "/dashboard":
                    self._send(
                        200,
                        render_dashboard(conn, days=self._int_param(params, "days", 7)).encode("utf-8"),
                        "text/html; charset=utf-8",
                    )
                    return
            finally:
                conn.close()
            self._json(404, {"ok": False, "error": "not found"})

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802 - stdlib signature
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path not in ("/ingest", "/ingest/shortcuts", "/ingest/hae", "/ingest/workouts", "/ingest/sleep"):
                self._json(404, {"ok": False, "error": f"unknown path {path}"})
                return
            if not self._authorized():
                return
            try:
                raw = self._read_body()
            except ValueError as exc:
                self._json(413, {"ok": False, "error": str(exc)})
                return
            params = self._params()
            source = params.get("source") or self.headers.get("X-Health-Source", "") or ""
            batch = parse_json_text(raw.decode("utf-8", "replace"), source=source)
            conn = state.connect()
            try:
                # /ingest/sleep and /ingest/workouts take the same body; the
                # path form exists so a shortcut can be explicit about it.
                if path == "/ingest/sleep" and not batch.sleep:
                    batch = parse_json_text(json.dumps({"sleep": _decode(raw)}), source=source)
                if path == "/ingest/workouts" and not batch.workouts:
                    batch = parse_json_text(json.dumps({"workouts": _decode(raw)}), source=source)

                samples = store.write_samples(conn, batch.samples, source=source)
                sleep = store.write_sleep(conn, batch.sleep, source=source)
                workouts = store.write_workouts(conn, batch.workouts, source=source)
                inserted = samples.inserted + sleep.inserted + workouts.inserted
                duplicates = samples.duplicates + sleep.duplicates + workouts.duplicates
                store.log_ingest(
                    conn,
                    kind=f"http{path}",
                    source=source or config.default_source(),
                    rows=inserted,
                    metrics=batch.summary()["metrics"],
                    detail=json.dumps({"duplicates": duplicates, "errors": batch.errors[:10]}),
                )
                state.maybe_prune(conn)
                # Echo a short receipt: the shortcut's last action shows it, so
                # "did it sync?" is answerable on the phone.
                self._json(
                    200,
                    {
                        "ok": True,
                        "received": batch.points,
                        "stored": inserted,
                        "duplicates": duplicates,
                        "sleep_spans": len(batch.sleep),
                        "workouts": len(batch.workouts),
                        "metrics": batch.summary()["metrics"],
                        "errors": batch.errors[:10],
                        "error_count": len(batch.errors),
                        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    },
                )
            finally:
                conn.close()

        def do_DELETE(self) -> None:  # noqa: N802
            self._json(405, {"ok": False, "error": "method not allowed"})

    return Handler


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace") or "[]")
    except ValueError:
        return []


def _lan_ip() -> Optional[str]:
    """Best-effort local address, for the startup banner only."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        address = sock.getsockname()[0]
        sock.close()
        return address
    except OSError:
        return None


def serve(
    db_path: Optional[str] = None,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    token: Optional[str] = None,
    verbose: bool = False,
    banner: bool = True,
) -> None:
    """Run the ingest server until interrupted."""
    resolved_db = str(config.db_path(db_path))
    bind_host = config.host(host)
    bind_port = config.port(port)
    secret = token if token is not None else config.token()

    state = Store(resolved_db)
    handler = make_handler(state, secret, verbose=verbose)
    httpd = ThreadingHTTPServer((bind_host, bind_port), handler)
    httpd.daemon_threads = True

    if banner:
        other = _lan_ip()
        print(f"hermes-health {__version__} ingest server", flush=True)
        print(f"  store     {resolved_db}", flush=True)
        print(f"  listening http://{bind_host}:{bind_port}", flush=True)
        if other and bind_host not in ("127.0.0.1", "localhost"):
            print(f"  reachable http://{other}:{bind_port}", flush=True)
        print(f"  auth      {'bearer token required' if secret else 'NONE (network is the auth layer)'}", flush=True)
        print(f"  timezone  {config.tz_name()}", flush=True)
        if not secret and bind_host not in ("127.0.0.1", "localhost"):
            print(
                "  warning   listening beyond loopback with no HERMES_HEALTH_TOKEN set;"
                " keep this port on a VPN/LAN and not the open internet",
                flush=True,
            )
        print("  post      POST /ingest  (Shortcuts body or Health Auto Export REST v2)", flush=True)
        sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[hermes-health] stopping", flush=True)
    finally:
        httpd.server_close()
