# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo import _loggers
from marimo._server.api.tools import tool
from marimo._server.api.utils import dispatch_control_request
from marimo._server.models.models import (
    StorageDownloadRequest,
    StorageListEntriesRequest,
    SuccessResponse,
)

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


@tool("storage_list_entries")
@requires("edit")
async def list_entries(
    *, request: Request, body: StorageListEntriesRequest
) -> SuccessResponse:
    """Ask the kernel to list the entries of a storage."""
    return await dispatch_control_request(request, body)


@tool("storage_download")
@requires("edit")
async def download(
    *, request: Request, body: StorageDownloadRequest
) -> SuccessResponse:
    """Ask the kernel to download an entry from a storage."""
    return await dispatch_control_request(request, body)
