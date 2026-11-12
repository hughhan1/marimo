# Copyright 2026 Marimo. All rights reserved.
"""The host API: its tools and events at `/mcp`, and the catalog's tools."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import msgspec
from starlette.routing import Route, Router

from marimo import _loggers
from marimo._core import protocol
from marimo._core.errors import (
    Conflict,
    Invalid,
    NotFound,
)
from marimo._core.operations import Reply
from marimo._server.api.auth import TOKEN_QUERY_PARAM
from marimo._server.api.deps import AppStateBase
from marimo._server.api.utils import format_url_host, open_url_in_browser
from marimo._server.host.notebook_api import _context, problem
from marimo._server.host.tools import host_mcp_endpoint
from marimo._server.tokens import AuthToken

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


def build_app() -> Router:
    """Build the host API router."""
    return Router(
        routes=[Route("/mcp", host_mcp_endpoint, methods=["POST"])],
        default=_not_found,
        # Clients never follow redirects, so a trailing slash is a miss.
        redirect_slashes=False,
    )


async def _not_found(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    await problem("not-found", "No such route")(scope, receive, send)


def _record(request: Request, notebook_id: str) -> protocol.Notebook:
    host = _context(request).bridge.host
    record = host.notebooks.get(notebook_id)
    if record is None:
        raise NotFound(f"no notebook {notebook_id}")
    return record


# ---- notebooks -----------------------------------------------------------------


def _inside_project(path: str) -> Path:
    if "\x00" in path:
        raise Invalid("path must not contain NUL")
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise Invalid("path must stay in the project")
    return relative


async def _create_notebook(
    request: Request, body: protocol.CreateNotebookRequest
) -> Reply:
    context = _context(request)
    key = body.idempotency_key

    index = context.bridge.index
    if index.root is None or body.project_id != index.project_id:
        raise NotFound(f"No project {body.project_id}")
    if body.path is msgspec.UNSET:
        raise Invalid("path is required")
    relative = _inside_project(body.path)
    file = index.root / relative
    notebook = await context.bridge.create(
        file,
        name=None if body.name is msgspec.UNSET else body.name,
        request_id=key,
    )
    return notebook


async def _update_notebook(
    request: Request, body: protocol.UpdateNotebookRequest
) -> Reply:
    context = _context(request)
    key = body.idempotency_key
    record = _record(request, body.notebook_id)

    if body.path is msgspec.UNSET and body.name is msgspec.UNSET:
        raise Invalid("path or name is required")
    index = context.bridge.index
    target = None
    if body.path is not msgspec.UNSET:
        if index.root is None:
            raise Conflict("The notebook has no file to move")
        relative = _inside_project(body.path)
        target = index.root / relative
    notebook = await context.bridge.update(
        record.id,
        target=target,
        name=None if body.name is msgspec.UNSET else body.name,
        request_id=key,
    )
    return notebook


async def _delete_notebook(
    request: Request, call: protocol.NotebookRequest
) -> Reply:
    context = _context(request)
    record = _record(request, call.notebook_id)
    await context.bridge.delete(record.id, request_id=call.idempotency_key)
    return None


async def _open_notebook(
    request: Request, call: protocol.NotebookRequest
) -> Reply:
    record = _record(request, call.notebook_id)
    file = _context(request).bridge.index.file_of(record.id)
    if file is None:
        raise Conflict("The notebook has no file to open")

    state = AppStateBase.from_app(request.app)
    authority = f"{format_url_host(state.host, state.port)}:{state.port}"
    url = f"http://{authority}{state.base_url}/?file={quote(str(file))}"
    auth_token = state.session_manager.auth_token
    if not AuthToken.is_empty(auth_token):
        url += f"&{TOKEN_QUERY_PARAM}={quote(str(auth_token))}"
    browser = state.config_manager.get_config().server.browser
    # Keep blocking browser discovery off the event loop.
    threading.Thread(
        target=_open_quietly, args=(browser, url), daemon=True
    ).start()
    return protocol.OpenNotebookResponse(url=url)


CATALOG: dict[str, Callable[[Request, Any], Awaitable[Reply]]] = {
    "notebook.create": _create_notebook,
    "notebook.update": _update_notebook,
    "notebook.delete": _delete_notebook,
    "notebook.open": _open_notebook,
}
"""The catalog's tools, which only the host's token may call."""


def _open_quietly(browser: str, url: str) -> None:
    try:
        open_url_in_browser(browser, url)
    except Exception as e:
        LOGGER.warning("Could not open the notebook in a browser: %s", e)


# ---- runtimes ------------------------------------------------------------------


# ---- runs ----------------------------------------------------------------------
