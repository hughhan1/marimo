# Copyright 2026 Marimo. All rights reserved.
"""The host context a request reaches, and problem responses for its streams."""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec
from starlette.responses import Response

from marimo import _loggers
from marimo._core import operations

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

    from marimo._server.host.context import HostContext

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
