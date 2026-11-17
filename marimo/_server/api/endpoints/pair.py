# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import msgspec
from starlette.authentication import requires

from marimo._messaging.messages import AGENT_MESSAGE_TYPE
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._utils.http import HTTPException, HTTPStatus

if TYPE_CHECKING:
    from starlette.requests import Request


class AgentMessage(msgspec.Struct):
    """Any JSON content for the agents attached to a notebook."""

    message: Any


@tool("pair_notebooks_messages", notebook="required")
@requires("edit")
async def messages(*, request: Request, body: AgentMessage) -> None:
    """Send any JSON content to all agents attached to the notebook."""
    state = AppState(request)
    notebook = state.notebook_for_session(state.require_current_session())
    if notebook is None or not any(
        a.kind == "agent" for a in notebook.attachments
    ):
        raise HTTPException(status_code=409, detail="No agent attached")
    notebook.emit(AGENT_MESSAGE_TYPE, body.message)


class DetachRequest(msgspec.Struct):
    """Select a host attachment to remove without stopping its notebook runtime."""

    attachment_id: str


@tool("pair_notebooks_attachments_detach", notebook="required")
@requires("edit")
async def detach(*, request: Request, body: DetachRequest) -> None:
    """Disconnect one attachment without stopping its notebook runtime."""
    state = AppState(request)
    if not body.attachment_id:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Invalid attachment ID.",
        )
    notebook = state.notebook_for_session(state.require_current_session())
    if notebook is not None:
        notebook.detach(body.attachment_id)
