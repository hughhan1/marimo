# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from marimo._types.lifespan import Lifespan

McpType = Literal["tools"]

if TYPE_CHECKING:
    from starlette.applications import Starlette


def setup_mcp_server(
    app: Starlette, *, allow_remote: bool = False
) -> Lifespan[Starlette]:
    """Set up the MCP server.

    Returns its lifespan, which must be included in the application's
    lifespan chain.
    """
    from marimo._mcp.server.lifespan import mcp_server_lifespan
    from marimo._mcp.server.main import setup_mcp_server as _setup_tools

    _setup_tools(app, allow_remote=allow_remote)
    return mcp_server_lifespan
