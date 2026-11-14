# Copyright 2026 Marimo. All rights reserved.
"""The host protocol's tools, served by the MCP SDK.

The host API serves every tool at its `/mcp` to the host record's token. The
editor serves the notebook tools beside its own at `/api/mcp`, behind its
own sign-in. A tool's arguments are its request type's fields, its result is
the reply, and a failure is a problem carried as the error's text.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

import msgspec
from starlette.requests import Request

from marimo import _loggers
from marimo._core.calls import Caller, decode, dispatch, once
from marimo._core.operations import problem
from marimo._core.protocol_tools import TOOLS
from marimo._messaging.notification import ConsumerCapabilities
from marimo._runtime.commands import HTTPRequest
from marimo._server.api.deps import AppState
from marimo._server.api.tools import (
    MCPEndpoint,
    schemas,
    tool_result,
    tool_server,
)
from marimo._server.host.auth import bearer_matches
from marimo._server.host.notebook_api import _context
from marimo._server.host.streams import host_events
from marimo._types.ids import ConsumerId

if TYPE_CHECKING:
    from mcp import types
    from mcp.server.context import ServerRequestContext

    from marimo._core.operations import Reply


LOGGER = _loggers.marimo_logger()


def _catalog() -> dict[str, Any]:
    from marimo._server.host.api import CATALOG

    return CATALOG


def notebook_tools() -> list[str]:
    """The tools that act on one notebook, which the editor also serves."""
    return [name for name in TOOLS if name not in _catalog()]


@cache
def definitions(*, catalog: bool) -> list[dict[str, Any]]:
    """The tools' definitions, as `tools/list` answers them."""
    names = list(TOOLS) if catalog else notebook_tools()
    return schemas({name: TOOLS[name] for name in names})


async def call(request: Request, name: str, arguments: object) -> Reply:
    """Serve one of the host's tools for `request`.

    A failure the host did not expect is an internal problem, which names
    nothing of its cause.
    """
    try:
        return await _serve(request, name, arguments)
    except Exception:
        LOGGER.exception("The host failed on %s", name)
        return problem("internal", "The host failed")


async def _serve(request: Request, name: str, arguments: object) -> Reply:
    try:
        args = decode(name, arguments)
    except msgspec.ValidationError as e:
        return problem("invalid-request", str(e))
    context = _context(request)
    serve = _catalog().get(name)
    if serve is not None:
        return await once(
            context.mutations, name, args, lambda: serve(request, args)
        )
    session = AppState(request).get_current_session()
    return await dispatch(
        context.mutations,
        context.bridge,
        name,
        args,
        Caller(
            request=_submitting_request(request),
            allow_input=session is not None
            and context.bridge.session_for_notebook(args.notebook_id)
            is session,
        ),
    )


async def editor_call(request: Request, name: str, arguments: object) -> Reply:
    """Serve a notebook tool to the signed-in editor."""
    if "edit" not in request.auth.scopes:
        return problem("forbidden", "This editor cannot edit notebooks")
    if not _connection_may_call(request, name):
        return problem(
            "forbidden", "This connection is read-only for this action."
        )
    return await call(request, name, arguments)


def _submitting_request(request: Request) -> HTTPRequest:
    """Describe the request to notebook code without the token that admitted it."""
    submitted = HTTPRequest.from_request(request)
    if bearer_matches(
        request.headers.get("authorization"), _context(request).token
    ):
        submitted.headers.pop("authorization", None)
    return submitted


def _connection_may_call(request: Request, name: str) -> bool:
    """Whether the editor's connection may call this notebook tool.

    Host tools are system-originated, so enforce connection capabilities
    here. A request naming no session relies on the editor's scope alone.
    """
    app_state = AppState(request)
    session_id = app_state.get_current_session_id()
    if session_id is None:
        return True
    session = app_state.get_current_session()
    if session is None:
        return False
    consumer = session.room.get_consumer(ConsumerId(session_id))
    capabilities = (
        session.room.get_capabilities(consumer)
        if consumer is not None
        else ConsumerCapabilities.VIEWER
    )
    return (
        capabilities.interact if name == "runtime.input" else capabilities.edit
    )


def mcp_endpoint() -> MCPEndpoint:
    async def call_tool(
        ctx: ServerRequestContext[Any, Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        request = ctx.request
        assert isinstance(request, Request)
        return tool_result(
            params.name,
            await call(request, params.name, params.arguments or {}),
        )

    server = tool_server(lambda: definitions(catalog=True), call_tool)
    host_events().serve(server)
    return MCPEndpoint(server)
