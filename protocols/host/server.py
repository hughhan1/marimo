# Copyright 2026 Marimo. All rights reserved.
"""The host protocol's tools, as the MCP server `holon types generate` loads.

It reads their contract from this server to write `holon-contract.json` and
the typed client. Where marimo is not installed, it reads the committed
contract instead.
"""

from __future__ import annotations

from marimo._server.host.tools import mcp_server

server = mcp_server()
