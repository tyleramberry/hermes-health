#!/usr/bin/env python3
"""Run the stdio MCP server straight from a checkout.

Point any MCP harness at this file:

    {"mcpServers": {"health": {"command": "python3",
                               "args": ["/path/to/hermes-health/health_mcp.py"]}}}
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hermes_health.mcp_server import main  # noqa: E402  (after sys.path fix)

if __name__ == "__main__":
    raise SystemExit(main())
