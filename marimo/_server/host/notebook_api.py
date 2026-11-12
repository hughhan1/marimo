# Copyright 2026 Marimo. All rights reserved.
"""Tool handlers for the notebook operations, and problem responses."""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any

import msgspec
from starlette.responses import Response

from marimo import _loggers
from marimo._core import operations
from marimo._core.calls import InputCall, dispatch
from marimo._core.errors import HostError
from marimo._messaging.notification import ConsumerCapabilities
from marimo._runtime.commands import HTTPRequest
from marimo._server.api.deps import AppState
from marimo._server.host.auth import bearer_matches
from marimo._types.ids import ConsumerId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import HTTPConnection, Request

    from marimo._core import protocol
    from marimo._core.calls import OperationCall
    from marimo._core.operations import Reply
    from marimo._server.host.context import HostContext

    Handler = Callable[[Request, Any], Awaitable[Reply]]
    """A tool handler: the endpoint's request and the decoded call, to a reply."""

LOGGER = _loggers.marimo_logger()


def _context(request: HTTPConnection) -> HostContext:
    context = getattr(request.app.state, "host_context", None)
    assert context is not None, "HostApiMiddleware admits requests only then"
    return context  # type: ignore[no-any-return]


def problem(kind: operations.PlainKind, detail: str | None = None) -> Response:
    """A problem response, for the streams the host serves over HTTP."""
    body = operations.problem(kind, detail)
    return Response(
        msgspec.json.encode(body),
        status_code=body.status,
        media_type="application/problem+json",
    )


_ROUTER_KINDS: dict[int, operations.PlainKind] = {
    400: "invalid-request",
    404: "not-found",
    405: "method-not-allowed",
    409: "conflict",
}


def status_problem(status: int, detail: str | None = None) -> Response:
    """Describe a status that the router or a route raised."""
    kind = _ROUTER_KINDS.get(status)
    if kind is None:
        LOGGER.error("The host API raised an unmapped status %s", status)
        return problem("internal", "The host failed")
    return problem(kind, detail)


def _mutation(operation: str) -> Callable[[Handler], Handler]:
    """Use the same receipt store for catalog operations implemented by the server."""

    def decorate(endpoint: Handler) -> Handler:
        @wraps(endpoint)
        async def handle(request: Request, call: Any) -> Reply:
            params = getattr(call, "params", None)
            try:
                return await _context(request).mutations.execute(
                    operation,
                    notebook_id=getattr(params, "notebook_id", None),
                    key=call.headers.idempotency_key,
                    payload=call,
                    call=lambda: endpoint(request, call),
                )
            except HostError as e:
                return operations.problem_for(e)

        return handle

    return decorate


async def serve(request: Request, call: OperationCall) -> Reply:
    """Dispatch a notebook operation's call to its bridge."""
    context = _context(request)
    session = AppState(request).get_current_session()
    return await dispatch(
        context.mutations,
        context.bridge,
        call,
        request=_submitting_request(request),
        allow_input=(
            session is not None
            and context.bridge.session_for_notebook(call.params.notebook_id)
            is session
        ),
    )


def _submitting_request(request: Request) -> HTTPRequest:
    """Describe the request to notebook code without the token that admitted it."""
    submitted = HTTPRequest.from_request(request)
    if bearer_matches(
        request.headers.get("authorization"), _context(request).token
    ):
        submitted.headers.pop("authorization", None)
    return submitted


def editor_refusal(
    request: Request, kind: type[OperationCall]
) -> protocol.Problem | None:
    """Why the signed-in editor may not call this notebook operation."""
    # The editor's middleware has already authenticated the request.
    scopes = request.auth.scopes
    if not scopes:
        return operations.problem("unauthorized", "Sign in to the editor")
    if "edit" not in scopes:
        return operations.problem(
            "forbidden", "This editor cannot edit notebooks"
        )
    if not _connection_may_call(request, kind):
        return operations.problem(
            "forbidden", "This connection is read-only for this action."
        )
    return None


def _connection_may_call(request: Request, kind: type[OperationCall]) -> bool:
    """Whether the connection may issue this notebook operation.

    Host commands are system-originated, so enforce connection capabilities
    here. Headerless requests rely on the editor's scope alone.
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
    return capabilities.interact if kind is InputCall else capabilities.edit
