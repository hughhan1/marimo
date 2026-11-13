# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.models.lsp import (
    LspHealthResponse,
    LspRestartRequest,
    LspRestartResponse,
)

if TYPE_CHECKING:
    from starlette.requests import Request


@tool("lsp_health")
@requires("edit")
async def lsp_health(*, request: Request) -> LspHealthResponse:
    """Report the health of the language servers."""
    app_state = AppState(request)
    return await app_state.session_manager.lsp_server.get_health()


@tool("lsp_restart")
@requires("edit")
async def lsp_restart(
    *, request: Request, body: LspRestartRequest
) -> LspRestartResponse:
    """Restart language servers, all of them or the ones named."""
    app_state = AppState(request)
    return await app_state.session_manager.lsp_server.restart(
        server_ids=body.server_ids
    )
