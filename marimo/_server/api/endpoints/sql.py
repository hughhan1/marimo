# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo._server.api.tools import tool
from marimo._server.api.utils import dispatch_control_request
from marimo._server.models.models import (
    SuccessResponse,
    ValidateSQLRequest,
)

if TYPE_CHECKING:
    from starlette.requests import Request


@tool("sql_validate")
@requires("edit")
async def validate_sql(
    *, request: Request, body: ValidateSQLRequest
) -> SuccessResponse:
    """Ask the kernel to validate a SQL query."""
    return await dispatch_control_request(request, body)
