# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from starlette.authentication import requires
from starlette.responses import StreamingResponse

from marimo import _loggers
from marimo._runtime.commands import HTTPRequest, UpdateUIElementCommand
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.api.utils import (
    dispatch_control_request,
    enforce_consumer_capability,
    get_code_mode_credentials,
    parse_request,
)
from marimo._server.models.models import (
    BaseResponse,
    DebugCellRequest,
    ExecuteScratchpadRequest,
    InvokeFunctionRequest,
    KernelStatusResponse,
    ModelRequest,
    SetBreakpointsRequest,
    SuccessResponse,
)
from marimo._server.router import APIRouter
from marimo._server.sse import wait_for_http_disconnect
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
from marimo._utils.asyncio_utils import cancel_and_wait
from marimo._utils.http import HTTPException

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()

# Router for execution endpoints
router = APIRouter()


@tool(
    "kernel_set_ui_element_value",
    input=UpdateUIElementValuesRequest,
    output=SuccessResponse,
)
@requires("read")
async def set_ui_element_values(
    *,
    request: Request,
    body: UpdateUIElementValuesRequest,
) -> BaseResponse:
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


@tool("kernel_set_model_value", input=ModelRequest, output=SuccessResponse)
@requires("read")
async def set_model_values(
    *,
    request: Request,
    body: ModelRequest,
) -> BaseResponse:
    """Send a model message to the kernel's widget."""
    return await dispatch_control_request(request, body)


@tool(
    "kernel_instantiate",
    input=InstantiateNotebookRequest,
    output=SuccessResponse,
)
@requires("edit")
async def instantiate(
    *,
    request: Request,
    body: InstantiateNotebookRequest,
) -> BaseResponse:
    """Start the notebook with its UI elements' initial values."""
    app_state = AppState(request)
    app_state.require_current_session().instantiate(
        body,
        http_request=HTTPRequest.from_request(request),
    )

    return SuccessResponse()


@tool(
    "kernel_function_call", input=InvokeFunctionRequest, output=SuccessResponse
)
@requires("read")
async def function_call(
    *,
    request: Request,
    body: InvokeFunctionRequest,
) -> BaseResponse:
    """Call a function a UI element registered with the kernel."""
    return await dispatch_control_request(request, body)


@tool("kernel_status", output=KernelStatusResponse)
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


@router.post("/execute", include_in_schema=False)
@requires("edit")
async def execute_code(
    *,
    request: Request,
) -> StreamingResponse:
    from marimo._runtime.commands import ExecuteScratchpadCommand
    from marimo._server.scratchpad import (
        ScratchCellListener,
        build_done_event,
        build_scratchpad_request,
        snapshot_for_scratchpad,
    )

    app_state = AppState(request)
    body = await parse_request(request, cls=ExecuteScratchpadRequest)
    session = app_state.require_current_session_with_stable_id(
        allow_notebook_id=True
    )

    # Register cells into the graph without executing them so that
    # code_mode's run_cell can resolve dependencies. The kernel
    # no-ops if already instantiated.
    session.instantiate(
        InstantiateNotebookRequest(object_ids=[], values=[], auto_run=False),
        http_request=HTTPRequest.from_request(request),
    )

    async def sse_generator() -> AsyncGenerator[str, None]:
        interrupt_sent = False

        def interrupt_once() -> None:
            # The disconnect watcher and the response cancellation can
            # both observe one client disconnect. The kernel must get
            # one interrupt. Both callers run on the event-loop thread,
            # so a plain flag is enough.
            nonlocal interrupt_sent
            if interrupt_sent:
                return
            interrupt_sent = True
            session.try_interrupt()

        async def watch_disconnect() -> None:
            await wait_for_http_disconnect(request)
            interrupt_once()

        disconnect_task = asyncio.create_task(watch_disconnect())
        # Correlation ID: tags both the scratchpad command and the
        # listener so we wait for *our* completion and ignore
        # `CompletedRun` events from other commands on this session
        # (e.g. the `session.instantiate` call above, or concurrent
        # browser activity).
        run_id = str(uuid4())
        try:
            listener = ScratchCellListener(run_id=run_id)
            # Ensure we take a lock on the scratchpad before scoping the
            # listener. See #10035.
            async with session.scratchpad_lock:
                with session.scoped(listener):
                    server_url, auth_token = get_code_mode_credentials(
                        app_state, request
                    )
                    http_req = build_scratchpad_request(
                        session,
                        request,
                        server_url=server_url,
                        auth_token=auth_token,
                    )
                    notebook_cells, cell_outputs = snapshot_for_scratchpad(
                        session
                    )
                    session.put_control_request(
                        ExecuteScratchpadCommand(
                            code=body.code,
                            request=http_req,
                            notebook_cells=notebook_cells,
                            cell_outputs=cell_outputs,
                            run_id=run_id,
                        ),
                        from_consumer_id=None,
                    )
                    async for event in listener.stream():
                        yield event

                yield build_done_event(session, listener)
        except asyncio.CancelledError:
            # On ASGI spec < 2.4 (uvicorn), Starlette cancels this
            # generator when the client disconnects. On spec >= 2.4 it
            # does not, and only the watcher fires. Interrupt here too
            # so both server kinds stop the kernel.
            interrupt_once()
            raise
        finally:
            await cancel_and_wait(disconnect_task)

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@tool(
    "kernel_scratchpad_run",
    input=ExecuteScratchpadRequest,
    output=SuccessResponse,
)
@requires("edit")
async def run_scratchpad(
    *,
    request: Request,
    body: ExecuteScratchpadRequest,
) -> BaseResponse:
    """Run code in the scratchpad, outside the notebook's cells."""
    return await dispatch_control_request(request, body)


@tool("kernel_pdb_pm", input=DebugCellRequest, output=SuccessResponse)
@requires("edit")
async def run_post_mortem(
    *,
    request: Request,
    body: DebugCellRequest,
) -> BaseResponse:
    """Start a post-mortem debugger on a cell's last error."""
    return await dispatch_control_request(request, body)


@tool(
    "kernel_pdb_breakpoints",
    input=SetBreakpointsRequest,
    output=SuccessResponse,
)
@requires("edit")
async def set_breakpoints(
    *,
    request: Request,
    body: SetBreakpointsRequest,
) -> BaseResponse:
    """Set the debugger's breakpoints in a cell."""
    return await dispatch_control_request(request, body)


@tool("kernel_restart_session", output=SuccessResponse)
@requires("edit")
async def restart_session(
    *,
    request: Request,
) -> BaseResponse:
    """Close the session so the client's reload starts a fresh kernel."""
    app_state = AppState(request)
    session_manager = app_state.session_manager

    # This just closes the session, and the frontend will
    # do a full reload, which will restart the session.
    session_id = app_state.require_current_session_id()
    app_state.require_current_session()
    session_manager.close_session(session_id)
    return SuccessResponse()


@tool("kernel_shutdown", output=SuccessResponse)
@requires("edit")
async def shutdown(
    *,
    request: Request,
) -> BaseResponse:
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


@tool("kernel_takeover", output=SuccessResponse)
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
