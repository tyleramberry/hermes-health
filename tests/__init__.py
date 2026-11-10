"""Tests for hermes-health. Standard library only:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Deterministic day attribution in tests, regardless of the host's zone.
os.environ.setdefault("HERMES_HEALTH_TZ", "America/Denver")


def load_plugin_package():
    """Import the repo root the way Hermes does: as a package with submodule search.

    That is the load path the tools actually take, so a relative-import mistake
    fails here rather than in the user's session.
    """
    import importlib.util

    module_name = "hermes_health_plugin_under_test"
    init_file = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(ROOT)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    """Stands in for Hermes' PluginContext: records what a plugin registers."""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.tools = {}
        self.commands = {}
        self.skills = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}
        return None

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler
        return None

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = Path(path)
        return None
