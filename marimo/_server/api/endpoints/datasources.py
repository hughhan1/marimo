# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo import _loggers
from marimo._server.api.tools import tool
from marimo._server.api.utils import dispatch_control_request
from marimo._server.models.models import (
    DiscoverDataSourcesRequest,
    ListDataSourceConnectionRequest,
    ListSQLSchemasRequest,
    ListSQLTablesRequest,
    PreviewDatasetColumnRequest,
    PreviewSQLTableRequest,
    SuccessResponse,
)

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


@tool("datasources_discover")
@requires("edit")
async def discover_data_sources(
    *, request: Request, body: DiscoverDataSourcesRequest
) -> SuccessResponse:
    """Ask the kernel to discover the notebook's data sources."""
    return await dispatch_control_request(request, body)


@tool("datasources_preview_column")
@requires("edit")
async def preview_column(
    *, request: Request, body: PreviewDatasetColumnRequest
) -> SuccessResponse:
    """Ask the kernel to preview a column of a dataset."""
    return await dispatch_control_request(request, body)


@tool("datasources_preview_sql_table")
@requires("edit")
async def preview_sql_table(
    *, request: Request, body: PreviewSQLTableRequest
) -> SuccessResponse:
    """Ask the kernel to preview a SQL table."""
    return await dispatch_control_request(request, body)


@tool("datasources_preview_sql_table_list")
@requires("edit")
async def preview_sql_table_list(
    *, request: Request, body: ListSQLTablesRequest
) -> SuccessResponse:
    """Ask the kernel to list the tables of a SQL schema."""
    return await dispatch_control_request(request, body)


@tool("datasources_preview_sql_schema_list")
@requires("edit")
async def preview_sql_schema_list(
    *, request: Request, body: ListSQLSchemasRequest
) -> SuccessResponse:
    """Ask the kernel to list the schemas of a SQL database."""
    return await dispatch_control_request(request, body)


@tool("datasources_preview_datasource_connection")
@requires("edit")
async def preview_datasource_connection(
    *, request: Request, body: ListDataSourceConnectionRequest
) -> SuccessResponse:
    """Ask the kernel to describe a data source connection."""
    return await dispatch_control_request(request, body)
