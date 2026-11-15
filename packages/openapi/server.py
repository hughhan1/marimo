# Copyright 2026 Marimo. All rights reserved.
"""The editor's tools, as the MCP server `holon types generate` loads to
write the frontend's typed client."""

from __future__ import annotations

# Importing the routes registers the tools.
from marimo._server.api.router import build_routes  # noqa: F401
from marimo._server.api.tools import mcp_server

server = mcp_server()
