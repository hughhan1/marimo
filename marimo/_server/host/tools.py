# Copyright 2026 Marimo. All rights reserved.
"""The Host Protocol's tools, served at the server's MCP endpoint.

A tool's arguments are its request type's fields, its result is the reply,
and a failure is a problem carried as the error's text. Only an editor may
call them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec

from marimo import _loggers
from marimo._core.calls import Caller, decode, dispatch, once
from marimo._core.operations import problem
from marimo._runtime.commands import HTTPRequest
from marimo._server.api.auth import bearer_matches
from marimo._server.api.deps import AppState
from marimo._server.api.utils import tab_capabilities
from marimo._server.host.api import CATALOG
from marimo._server.host.context import host_context

if TYPE_CHECKING:
    from starlette.requests import Request

    from marimo._core.operations import Reply


LOGGER = _loggers.marimo_logger()


async def call(request: Request, name: str, arguments: object) -> Reply:
    """Serve one of the host's tools for `request`.

    A failure the host did not expect is an internal problem, which names
    nothing of its cause.
    """
    if "edit" not in request.auth.scopes:
        return problem("forbidden", "This editor cannot edit notebooks")
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
    context = host_context(request)
    serve = CATALOG.get(name)
    if serve is not None:
        return await once(
            context.mutations, name, args, lambda: serve(request, args)
        )
    tab = AppState(request).tab
    if tab is not None:
        capabilities = tab_capabilities(
            context.bridge.session_for_notebook(args.notebook_id), tab
        )
        if not (
            capabilities.interact
            if name == "runtime.input"
            else capabilities.edit
        ):
            return problem(
                "forbidden", "This connection is read-only for this action."
            )
    return await dispatch(
        context.mutations,
        context.bridge,
        name,
        args,
        Caller(
            request=_submitting_request(request),
            allow_input=tab is not None,
        ),
    )


def _submitting_request(request: Request) -> HTTPRequest:
    """Describe the request to notebook code without the token that admitted it."""
    submitted = HTTPRequest.from_request(request)
    if bearer_matches(
        request.headers.get("authorization", ""),
        str(AppState(request).session_manager.auth_token),
    ):
        submitted.headers.pop("authorization", None)
    return submitted
