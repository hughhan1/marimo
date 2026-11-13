# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo._runtime.commands import ClearCacheCommand, GetCacheInfoCommand
from marimo._server.api.tools import tool
from marimo._server.api.utils import dispatch_control_request
from marimo._server.models.models import (
    ClearCacheRequest,
    GetCacheInfoRequest,
    SuccessResponse,
)

if TYPE_CHECKING:
    from starlette.requests import Request


@tool("cache_clear")
@requires("edit")
async def clear_cache(
    *, request: Request, body: ClearCacheRequest
) -> SuccessResponse:
    """Clear the kernel's cache."""
    del body
    return await dispatch_control_request(request, ClearCacheCommand())


@tool("cache_info")
@requires("edit")
async def get_cache_info(
    *, request: Request, body: GetCacheInfoRequest
) -> SuccessResponse:
    """Ask the kernel to report on its cache."""
    del body
    return await dispatch_control_request(request, GetCacheInfoCommand())
