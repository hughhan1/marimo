# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo import _loggers
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._snippets.snippets import Snippets, read_snippets

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


# Cache snippets once per server instance
_SNIPPETS: list[Snippets] = []


@tool("documentation_snippets", output=Snippets)
@requires("edit")
async def load_snippets(
    *,
    request: Request,
) -> Snippets:
    """List the code snippets the editor offers."""
    app_state = AppState(request)
    config = app_state.require_current_session().config_manager.get_config()
    if not _SNIPPETS:
        _SNIPPETS.append(await read_snippets(config))
    return _SNIPPETS[0]
