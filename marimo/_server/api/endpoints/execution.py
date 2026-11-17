# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from starlette.authentication import requires

from marimo import _loggers
from marimo._ai._tools.types import CodeExecutionResult
from marimo._runtime.commands import (
    DebugCellCommand,
    ExecuteScratchpadCommand,
    HTTPRequest,
    SetBreakpointsCommand,
    UpdateUIElementCommand,
)
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.api.utils import (
    dispatch_control_request,
    enforce_consumer_capability,
    get_code_mode_credentials,
)
from marimo._server.models.models import (
    DebugCellRequest,
    ExecuteScratchpadRequest,
    InvokeFunctionRequest,
    KernelStatusResponse,
    ModelRequest,
    SetBreakpointsRequest,
    SuccessResponse,
)
from marimo._server.uvicorn_utils import close_uvicorn
from marimo._session.consumer_policy import (
    TakeoverDecision,
    can_take_over_editing,
)
from marimo._session.requests import (
    InstantiateNotebookRequest,
    UpdateUIElementValuesRequest,
)
from marimo._session.types import KernelState
from marimo._types.ids import ConsumerId
from marimo._utils.http import HTTPException

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


@tool("kernel_set_ui_element_value")
@requires("read")
async def set_ui_element_values(
    *,
    request: Request,
    body: UpdateUIElementValuesRequest,
) -> SuccessResponse:
    """Set UI elements' values, which runs the cells that read them."""
    app_state = AppState(request)
    command = UpdateUIElementCommand(
        object_ids=body.object_ids,
        values=body.values,
        token=str(uuid4()),
        request=HTTPRequest.from_request(request),
    )
    enforce_consumer_capability(app_state, command)
    app_state.require_current_session().put_control_request(
        command,
        from_consumer_id=ConsumerId(app_state.require_current_session_id()),
    )

    return SuccessResponse()


@tool("kernel_set_model_value")
@requires("read")
async def set_model_values(
    *,
    request: Request,
    body: ModelRequest,
) -> SuccessResponse:
    """Send a model message to the kernel's widget."""
    return await dispatch_control_request(request, body)


@tool("kernel_instantiate")
@requires("edit")
async def instantiate(
    *,
    request: Request,
    body: InstantiateNotebookRequest,
) -> SuccessResponse:
    """Start the notebook with its UI elements' initial values."""
    app_state = AppState(request)
    app_state.require_current_session().instantiate(
        body,
        http_request=HTTPRequest.from_request(request),
    )

    return SuccessResponse()


@tool("kernel_function_call")
@requires("read")
async def function_call(
    *,
    request: Request,
    body: InvokeFunctionRequest,
) -> SuccessResponse:
    """Call a function a UI element registered with the kernel."""
    return await dispatch_control_request(request, body)


@tool("kernel_status")
@requires("edit")
async def kernel_status(
    *,
    request: Request,
) -> KernelStatusResponse:
    """Report whether the kernel is stopped, idle or running cells."""
    app_state = AppState(request)
    session = app_state.require_current_session()

    if session.kernel_state() in (
        KernelState.STOPPED,
        KernelState.NOT_STARTED,
    ):
        return KernelStatusResponse(state="stopped")

    is_running = any(
        notification.status in ("queued", "running")
        for notification in session.session_view.cell_notifications.values()
    )
    return KernelStatusResponse(state="running" if is_running else "idle")


@tool("kernel_execute")
@requires("edit")
async def execute_code(
    *,
    request: Request,
    body: ExecuteScratchpadRequest,
) -> CodeExecutionResult:
    """Run code against the notebook's live kernel and return its outcome."""
    from marimo._server.scratchpad import run_scratchpad_code

    app_state = AppState(request)
    session = app_state.require_current_session_with_stable_id(
        allow_notebook_id=True
    )
    server_url, auth_token = get_code_mode_credentials(app_state, request)
    return await run_scratchpad_code(
        session,
        request,
        code=body.code,
        server_url=server_url,
        auth_token=auth_token,
        timeout=None,
    )


@tool("kernel_scratchpad_run")
@requires("edit")
async def run_scratchpad(
    *,
    request: Request,
    body: ExecuteScratchpadRequest,
) -> SuccessResponse:
    """Run code in the scratchpad, outside the notebook's cells."""
    return await dispatch_control_request(
        request,
        ExecuteScratchpadCommand(
            code=body.code, request=HTTPRequest.from_request(request)
        ),
    )


@tool("kernel_pdb_pm")
@requires("edit")
async def run_post_mortem(
    *,
    request: Request,
    body: DebugCellRequest,
) -> SuccessResponse:
    """Start a post-mortem debugger on a cell's last error."""
    return await dispatch_control_request(
        request,
        DebugCellCommand(
            cell_id=body.cell_id, request=HTTPRequest.from_request(request)
        ),
    )


@tool("kernel_pdb_breakpoints")
@requires("edit")
async def set_breakpoints(
    *,
    request: Request,
    body: SetBreakpointsRequest,
) -> SuccessResponse:
    """Set the debugger's breakpoints in a cell."""
    return await dispatch_control_request(
        request,
        SetBreakpointsCommand(
            breakpoints=body.breakpoints,
            request=HTTPRequest.from_request(request),
        ),
    )


@tool("kernel_restart_session")
@requires("edit")
async def restart_session(
    *,
    request: Request,
) -> SuccessResponse:
    """Close the session so the client's reload starts a fresh kernel."""
    app_state = AppState(request)
    session_manager = app_state.session_manager

    # This just closes the session, and the frontend will
    # do a full reload, which will restart the session.
    session_id = app_state.require_current_session_id()
    app_state.require_current_session()
    session_manager.close_session(session_id)
    return SuccessResponse()


@tool("kernel_shutdown")
@requires("edit")
async def shutdown(
    *,
    request: Request,
) -> SuccessResponse:
    """Shut down the session, and the server when it was the last one."""
    LOGGER.debug("Received shutdown request")
    app_state = AppState(request)
    session_manager = app_state.session_manager
    workspace = session_manager.workspace

    async def shutdown_server() -> None:
        await app_state.session_manager.shutdown()
        close_uvicorn(app_state.server)

    # If we are only operating on a single file (new or explicit file),
    # and there are no other sessions (user may have opened another notebook
    # from the file explorer) then we should shutdown the whole server
    key = workspace.get_unique_file_key()
    if key and len(session_manager.sessions) <= 1:
        await shutdown_server()
        return SuccessResponse()

    # Otherwise, get the session
    session_id = app_state.get_current_session_id()
    if not session_id:
        await shutdown_server()
        return SuccessResponse()

    was_shutdown = session_manager.close_session(session_id)
    if not was_shutdown:
        await shutdown_server()

    return SuccessResponse()


@tool("kernel_takeover")
@requires("edit")
async def takeover_endpoint(
    *,
    request: Request,
) -> SuccessResponse:
    """Make the calling client the notebook's editor."""
    app_state = AppState(request)

    session_id = app_state.get_current_session_id()
    if session_id is None:
        LOGGER.error("Missing Marimo-Session-Id header")
        raise HTTPException(
            status_code=400, detail="Cannot take over session."
        )
    caller_id = ConsumerId(session_id)

    session = app_state.get_current_session()
    if not session:
        LOGGER.error("No current session found")
        raise HTTPException(
            status_code=400, detail="Cannot take over session."
        )

    caller = session.room.get_consumer(caller_id)
    if not caller:
        LOGGER.error("No consumer found for caller ID %s", caller_id)
        raise HTTPException(
            status_code=400, detail="Cannot take over session."
        )

    takeover_decision = can_take_over_editing(session, caller_id)
    if takeover_decision != TakeoverDecision.ALLOW:
        LOGGER.debug("Takeover denied by policy for consumer %s", caller_id)
        raise HTTPException(
            status_code=403, detail="Not allowed to take over session."
        )

    session.room.promote_consumer_to_main(caller)

    return SuccessResponse()
