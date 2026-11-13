# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from starlette.authentication import requires
from starlette.exceptions import HTTPException

from marimo import _loggers
from marimo._runtime.commands import RenameNotebookCommand
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.api.utils import enforce_consumer_capability
from marimo._server.files.path_validator import PathValidator
from marimo._server.models.models import (
    CopyNotebookRequest,
    ReadCodeResponse,
    RenameNotebookRequest,
    SaveAppConfigurationRequest,
    SaveLayoutRequest,
    SuccessResponse,
    TextResponse,
)
from marimo._types.ids import ConsumerId
from marimo._utils.async_path import abspath
from marimo._utils.http import HTTPStatus

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


@tool("kernel_read_code")
@requires("read")
async def read_code(
    *,
    request: Request,
) -> ReadCodeResponse:
    """Read the notebook file's contents."""
    app_state = AppState(request)

    # Check if code should be visible (edit mode or include_code=True)
    if not app_state.session_manager.should_send_code_to_frontend():
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Code is not available",
        )

    session = app_state.require_current_session()

    if not session.app_file_manager.path:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must be saved before downloading",
        )

    contents = session.app_file_manager.read_file()

    return ReadCodeResponse(contents=contents)


@tool("kernel_rename")
@requires("edit")
async def rename_file(
    *,
    request: Request,
    body: RenameNotebookRequest,
) -> SuccessResponse:
    """Rename or move the notebook file."""
    app_state = AppState(request)
    directory = app_state.session_manager.workspace.directory

    # Resolve relative filenames against the workspace's directory
    if not Path(body.filename).is_absolute() and directory:
        body.filename = str(Path(directory) / body.filename)

    filename = await abspath(body.filename)

    # Prevent path traversal: ensure target is inside the router's directory
    if directory:
        PathValidator().validate_inside_directory(
            Path(directory), Path(filename)
        )

    command = RenameNotebookCommand(filename=filename)
    enforce_consumer_capability(app_state, command)
    success, error = await app_state.session_manager.rename_session(
        app_state.require_current_session_id(), body.filename
    )
    if not success:
        raise HTTPException(status_code=400, detail=error)

    # Rename-triggered cells may immediately read the file or edit its
    # dependencies. The durable file and server binding must be ready first.
    app_state.require_current_session().put_control_request(
        command,
        from_consumer_id=ConsumerId(app_state.require_current_session_id()),
    )

    return SuccessResponse()


@tool("kernel_save_layout")
@requires("edit")
async def save_layout(
    *,
    request: Request,
    body: SaveLayoutRequest,
) -> SuccessResponse:
    """Save the notebook's layout beside it."""
    app_state = AppState(request)
    app_state.require_current_session().app_file_manager.save_layout(
        body.layout
    )
    return SuccessResponse()


@tool("kernel_copy")
@requires("edit")
async def copy(
    *,
    request: Request,
    body: CopyNotebookRequest,
) -> TextResponse:
    """Copy the notebook file and return the copy's contents."""
    app_state = AppState(request)

    # Resolve relative filenames against the workspace's directory
    directory = app_state.session_manager.workspace.directory
    if directory:
        if body.source and not Path(body.source).is_absolute():
            body.source = str(Path(directory) / body.source)
        if body.destination and not Path(body.destination).is_absolute():
            body.destination = str(Path(directory) / body.destination)

    # Prevent path traversal: ensure source and destination are inside the router's directory
    if directory:
        validator = PathValidator()
        if body.source:
            validator.validate_inside_directory(
                Path(directory), Path(body.source)
            )
        if body.destination:
            validator.validate_inside_directory(
                Path(directory), Path(body.destination)
            )

    session = app_state.require_current_session()
    return TextResponse(text=session.app_file_manager.copy(body))


@tool("kernel_save_app_config")
@requires("edit")
async def save_app_config(
    *,
    request: Request,
    body: SaveAppConfigurationRequest,
) -> TextResponse:
    """Save the app's configuration into the notebook file and return the file."""
    app_state = AppState(request)
    session = app_state.require_current_session()
    return TextResponse(
        text=session.app_file_manager.save_app_config(body.config)
    )
