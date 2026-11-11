# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo._messaging.notification import FocusCellNotification
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.api.utils import (
    dispatch_control_request,
    enforce_consumer_capability,
    install_packages_on_server,
    notify_server_missing_packages,
)
from marimo._server.models.models import (
    BaseResponse,
    CodeCompletionRequest,
    FocusCellRequest,
    FormatCellsRequest,
    FormatResponse,
    InstallPackagesRequest,
    StdinRequest,
    SuccessResponse,
)
from marimo._utils.formatter import DefaultFormatter

if TYPE_CHECKING:
    from starlette.requests import Request


@tool(
    "kernel_code_autocomplete",
    input=CodeCompletionRequest,
    output=SuccessResponse,
)
@requires("edit")
async def code_complete(
    *, request: Request, body: CodeCompletionRequest
) -> BaseResponse:
    """Ask the kernel for code completions at a position in a cell."""
    return await dispatch_control_request(request, body)


@tool("kernel_focus_cell", input=FocusCellRequest, output=SuccessResponse)
@requires("edit")
async def focus_cell(
    *, request: Request, body: FocusCellRequest
) -> BaseResponse:
    """Tell the notebook's clients to focus a cell."""
    app_state = AppState(request)
    app_state.require_current_session().notify(
        FocusCellNotification(cell_id=body.cell_id),
        from_consumer_id=None,
    )
    return SuccessResponse()


@tool("kernel_format", input=FormatCellsRequest, output=FormatResponse)
@requires("edit")
async def format_cell(
    *, request: Request, body: FormatCellsRequest
) -> FormatResponse:
    """Format cells' code with the server's formatter."""
    app_state = AppState(request)
    formatter = DefaultFormatter(line_length=body.line_length)
    filename = app_state.require_current_session().app_file_manager.path
    if filename and filename.endswith((".md", ".qmd")):
        filename = f"{filename}.py"

    try:
        codes = await formatter.format(body.codes, filename)
        return FormatResponse(codes)
    except ModuleNotFoundError:
        # Sandboxed kernels cannot install the server's formatter.
        if app_state.session_manager.sandbox:
            raise ModuleNotFoundError(
                "Server does not have a formatter. Please install ruff"
            ) from None
        notify_server_missing_packages(
            app_state.get_current_session(),
            app_state.get_current_session_id(),
            ["ruff"],
        )
        # Re-raise without .name so the error handler returns 500 without
        # sending a duplicate notification.
        raise ModuleNotFoundError(
            "Server does not have a formatter. Please install ruff"
        ) from None


@tool("kernel_stdin", input=StdinRequest, output=SuccessResponse)
@requires("edit")
async def stdin(*, request: Request, body: StdinRequest) -> BaseResponse:
    """Answer the kernel's request for input."""
    app_state = AppState(request)
    app_state.require_current_session().put_input(body.text)

    return SuccessResponse()


@tool(
    "kernel_install_missing_packages",
    input=InstallPackagesRequest,
    output=SuccessResponse,
)
@requires("edit")
async def install_missing_packages(
    *, request: Request, body: InstallPackagesRequest
) -> BaseResponse:
    """Install packages into the kernel's environment, or the server's."""
    cmd = body.as_command()

    if cmd.source == "server":
        # Install into the server's own Python environment (sys.executable).
        # Used when the server itself needs a package (e.g. nbformat for
        # IPYNB auto-export when running with --sandbox).
        app_state = AppState(request)
        enforce_consumer_capability(app_state, cmd)
        if cmd.versions:
            await install_packages_on_server(cmd.versions)
        return SuccessResponse()

    # Default ("kernel"): dispatch to kernel via ZeroMQ control queue.
    return await dispatch_control_request(request, cmd)
