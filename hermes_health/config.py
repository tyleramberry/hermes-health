"""Runtime configuration: where the database lives, what timezone days use, how the listener binds.

Every value has an environment override so the pipeline works from a launchd job,
a cron job, or a shell without a config file anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib from 3.9
    ZoneInfo = None  # type: ignore[assignment]

ENV_DB = "HERMES_HEALTH_DB"
ENV_TZ = "HERMES_HEALTH_TZ"
ENV_HOST = "HERMES_HEALTH_HOST"
ENV_PORT = "HERMES_HEALTH_PORT"
ENV_TOKEN = "HERMES_HEALTH_TOKEN"
ENV_SOURCE = "HERMES_HEALTH_SOURCE"

DEFAULT_DB_DIR = Path.home() / ".hermes-health"
DEFAULT_DB_NAME = "health.db"


def default_db_path() -> Path:
    """``$HERMES_HEALTH_DB`` if set, else ``~/.hermes-health/health.db``."""
    override = os.environ.get(ENV_DB, "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_DB_DIR / DEFAULT_DB_NAME


def db_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return default_db_path()


def _system_tz_name() -> Optional[str]:
    """Best-effort IANA name for the host's local timezone."""
    env = os.environ.get("TZ", "").strip()
    if env:
        return env
    localtime = Path("/etc/localtime")
    try:
        target = os.path.realpath(str(localtime))
    except OSError:  # pragma: no cover
        return None
    marker = "/zoneinfo/"
    if marker in target:
        return target.split(marker, 1)[1]
    return None


def tz_name(explicit: Optional[str] = None) -> str:
    """Resolved timezone name, used to attribute a timestamp to a calendar day."""
    return (explicit or os.environ.get(ENV_TZ, "").strip() or _system_tz_name() or "UTC")


def tzinfo(explicit: Optional[str] = None):
    """A tzinfo for the configured zone, falling back to a fixed local offset.

    Day boundaries matter more than the exact zone object: an unknown name must
    never crash an ingest run, so the fallback is the host's current offset.
    """
    name = tz_name(explicit)
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().tzinfo


def host(explicit: Optional[str] = None) -> str:
    return explicit or os.environ.get(ENV_HOST, "").strip() or "127.0.0.1"


def port(explicit: Optional[object] = None) -> int:
    if explicit is not None:
        return int(explicit)
    raw = os.environ.get(ENV_PORT, "").strip()
    return int(raw) if raw else 8787


def token() -> str:
    """Shared secret for the ingest endpoint, empty when unset.

    Over a mesh VPN the network is already the auth layer; a token is for hosts
    that are reachable some other way.
    """
    return os.environ.get(ENV_TOKEN, "").strip()


def default_source() -> str:
    return os.environ.get(ENV_SOURCE, "").strip() or "phone"
