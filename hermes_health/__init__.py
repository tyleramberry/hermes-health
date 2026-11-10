"""hermes-health — Apple Health data for AI agents.

Phone -> this store -> any harness. HealthKit is device-local, so the phone
pushes (Shortcuts or a Health Auto Export REST automation) and everything in
here is the receiving side: normalize, aggregate, store, query.

No third-party dependencies. Runs on the system Python (3.9+) with the standard
library only, so the always-on host needs no package manager, no Homebrew, and
no virtualenv.
"""

__version__ = "1.0.0"

DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"

__all__ = ["__version__", "DEFAULT_PORT", "DEFAULT_HOST"]
