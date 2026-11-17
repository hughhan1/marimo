# Copyright 2026 Marimo. All rights reserved.
"""The editor's tools, as the MCP server `holon types generate` loads.

It reads their contract from this server to write `holon-contract.json` and
the typed client. Where marimo is not installed, it reads the committed
contract instead.
"""

from __future__ import annotations

# Importing the endpoints registers the tools.
import marimo._server.api.endpoints  # noqa: F401
from marimo._server.api.tools import MCPEndpoint

server = MCPEndpoint().server
