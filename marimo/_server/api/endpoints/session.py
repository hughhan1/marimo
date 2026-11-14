# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo._server.api.events import SessionRequest, SessionState
from marimo._server.api.tools import tool

if TYPE_CHECKING:
    from starlette.requests import Request


@tool("session_state")
@requires("read")
async def session_state(
    *, request: Request, body: SessionRequest
) -> SessionState:
    """Join a page's session and read it, as of the `session` event's cursor.

    Follow the `session` event from `cursor` for every change after it.
    """
    return await request.app.state.editor_mcp.session_events.state(
        request, body
    )
