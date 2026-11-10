"""Hermes plugin entry point.

Importing this file registers nothing; Hermes calls ``register(ctx)`` when the
plugin is enabled. Everything heavy lives in :mod:`plugin_tools`, so a plugin
that is installed but never used costs nothing at startup.
"""

from __future__ import annotations


def register(ctx):
    """Register the health tools, the ``/health`` command, and the bundled skill."""
    from .plugin_tools import register_all

    return register_all(ctx)
