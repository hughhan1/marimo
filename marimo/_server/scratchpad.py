# Copyright 2026 Marimo. All rights reserved.
"""Shared utilities for scratchpad execution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from marimo._ai._tools.types import CodeExecutionResult
from marimo._code_mode.screenshot_meta import (
    SCREENSHOT_AUTH_TOKEN_KEY,
    SCREENSHOT_FILE_KEY,
    SCREENSHOT_SERVER_URL_KEY,
)
from marimo._messaging.cell_output import CellChannel
from marimo._messaging.context import CODE_MODE_KEY
from marimo._messaging.errors import MarimoAncestorStoppedError
from marimo._messaging.notebook.outputs import CellOutputs, scratchpad_snapshot
from marimo._messaging.notification import (
    CellNotification,
    CompletedRunNotification,
)
from marimo._messaging.serde import deserialize_kernel_message
from marimo._runtime.commands import ExecuteScratchpadCommand, HTTPRequest
from marimo._runtime.scratch import SCRATCH_CELL_ID
from marimo._session.extensions.types import EventAwareExtension
from marimo._session.requests import InstantiateNotebookRequest

if TYPE_CHECKING:
    from starlette.requests import Request

    from marimo._messaging.notebook.document import (
        NotebookCell,
    )
    from marimo._messaging.types import KernelMessage
    from marimo._session.session import Session
    from marimo._types.encodable import Encodable

EXECUTION_TIMEOUT = 30.0


# -- Listeners ----------------------------------------------------------------


class ScratchCellListener(EventAwareExtension):
    """Listens for a scratchpad execution's notifications until it completes.

    In addition to the scratch cell's own output, the listener captures
    console output (stdout/stderr) from *other* cells that execute while
    the scratchpad is active — e.g. cells created and run by
    `_code_mode`.  The done sentinel fires on the
    `CompletedRunNotification` whose `run_id` matches this
    listener's `run_id` — other completion events (e.g. from the
    `session.instantiate` that happens before the scratchpad runs, or
    from concurrent browser activity) are ignored, so we can't
    misidentify someone else's completion as ours.
    """

    def __init__(self, *, run_id: str) -> None:
        super().__init__()
        self._done = asyncio.Event()
        self._run_id = run_id
        self._scratch_started = False
        self.timed_out = False
        self.child_error_summaries: list[str] = []
        self.stdout: list[str] = []
        self.stderr: list[str] = []

    def on_notification_sent(
        self, session: Session, notification: KernelMessage
    ) -> None:
        del session
        msg = deserialize_kernel_message(notification)

        # Completion sentinel: the `CompletedRunNotification` tagged
        # with OUR run_id means the scratchpad's full cascade (including
        # any `state_updates` flushed after the scratch cell goes
        # idle) has settled. `CompletedRun`s with other ids belong to
        # unrelated commands — skip them.
        if isinstance(msg, CompletedRunNotification):
            if msg.run_id == self._run_id:
                self._done.set()
            return

        if not isinstance(msg, CellNotification):
            return

        if msg.cell_id == SCRATCH_CELL_ID:
            self._scratch_started = True
        elif not self._scratch_started:
            # Ignore notifications from an execution that was already active
            # when this listener was attached. Child cells run by _code_mode
            # cannot execute before the scratch cell itself starts.
            return

        # Console output keeps arriving briefly after completion; see `wait`.
        if msg.console is not None:
            console = (
                msg.console if isinstance(msg.console, list) else [msg.console]
            )
            for output in console:
                if output is None:
                    continue
                if output.channel == CellChannel.STDOUT:
                    self.stdout.append(str(output.data))
                elif output.channel == CellChannel.STDERR:
                    self.stderr.append(str(output.data))

        # Freeze diagnostics at this request's completion boundary.
        if self._done.is_set():
            return

        if msg.cell_id != SCRATCH_CELL_ID:
            if (
                msg.output is not None
                and msg.output.channel == CellChannel.MARIMO_ERROR
                and isinstance(msg.output.data, list)
                and msg.output.data
            ):
                for err in msg.output.data:
                    # `mo.stop` is intentional control flow. The runtime marks
                    # cells downstream of the stopped cell with this structured
                    # error so the frontend can explain why they did not run,
                    # but it must not turn a successful scratchpad edit into a
                    # failed code-mode request.
                    if isinstance(err, MarimoAncestorStoppedError):
                        continue
                    exc_type = (
                        getattr(err, "exception_type", None)
                        or type(err).__name__
                    )
                    raw_detail = getattr(err, "msg", "")
                    detail = str(raw_detail or "").strip()
                    empty_detail = f"This cell raised an exception: {exc_type}"
                    if detail == empty_detail:
                        detail = ""
                    if detail == exc_type or detail.startswith(f"{exc_type}:"):
                        diagnostic = detail
                    elif detail:
                        diagnostic = f"{exc_type}: {detail}"
                    else:
                        diagnostic = exc_type
                    self.child_error_summaries.append(
                        f"cell '{msg.cell_id}' raised {diagnostic}"
                    )

    async def wait(self, timeout: float | None = EXECUTION_TIMEOUT) -> None:
        """Block until execution completes, or `timeout` seconds pass.

        Sets `self.timed_out` if the deadline passes first.
        """
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except TimeoutError:
            self.timed_out = True
            return
        # Console output is flushed every 10ms, so let trailing output arrive.
        await asyncio.sleep(0.05)


# -- Helpers ------------------------------------------------------------------


def build_scratchpad_request(
    session: Session,
    request: Request,
    *,
    server_url: str,
    auth_token: str,
) -> HTTPRequest:
    """Build kernel request metadata for scratchpad execution.

    Args:
        session (Session): Notebook that owns the scratchpad.
        request (Request): HTTP request to serialize.
        server_url (str): Trusted URL for screenshot callbacks.
        auth_token (str): Token for screenshot callbacks.
    """
    http_req = HTTPRequest.from_request(request)
    http_req.meta.update(
        scratchpad_request_metadata(
            session, server_url=server_url, auth_token=auth_token
        )
    )
    return http_req


def scratchpad_request_metadata(
    session: Session, *, server_url: str, auth_token: str
) -> dict[str, Encodable]:
    """Return server metadata for scratchpad screenshot callbacks.

    Args:
        session (Session): Session that owns the scratchpad.
        server_url (str): Trusted URL for screenshot callbacks.
        auth_token (str): Token for screenshot callbacks.
    """
    return {
        CODE_MODE_KEY: True,
        SCREENSHOT_SERVER_URL_KEY: server_url,
        SCREENSHOT_AUTH_TOKEN_KEY: auth_token,
        SCREENSHOT_FILE_KEY: session.app_file_manager.path
        or session.initialization_id,
    }


def snapshot_for_scratchpad(
    session: Session,
) -> tuple[tuple[NotebookCell, ...], CellOutputs]:
    """Snapshot the notebook document and per-cell outputs for code_mode.

    Passed on `ExecuteScratchpadCommand` so `cm.get_context()` can expose
    cells and their last outputs.
    """
    return scratchpad_snapshot(session.document, session.session_view)


def extract_result(
    session: Session,
    listener: ScratchCellListener | None = None,
) -> CodeExecutionResult:
    """Read the scratch cell's final state from the session view."""
    cell_notif = session.session_view.cell_notifications.get(SCRATCH_CELL_ID)
    if cell_notif is None:
        return CodeExecutionResult(success=True)

    output_data = None
    if cell_notif.output is not None:
        data = cell_notif.output.data
        if isinstance(data, str):
            output_data = data
        elif isinstance(data, dict):
            output_data = data.get(
                "text/plain", data.get("text/html", str(data))
            )

    stdout: list[str] = []
    stderr: list[str] = []
    for out in (
        cell_notif.console if isinstance(cell_notif.console, list) else []
    ):
        if out is None:
            continue
        if listener is not None:
            continue
        if out.channel == CellChannel.STDOUT:
            stdout.append(str(out.data))
        elif out.channel == CellChannel.STDERR:
            stderr.append(str(out.data))

    errors: list[str] = []
    if cell_notif.output is not None and isinstance(
        cell_notif.output.data, list
    ):
        for err in cell_notif.output.data:
            errors.append(str(getattr(err, "msg", None) or err))

    # Include child cell error summaries.
    if listener:
        stdout.extend(listener.stdout)
        stderr.extend(listener.stderr)
        errors.extend(listener.child_error_summaries)

    return CodeExecutionResult(
        success=len(errors) == 0,
        output=output_data,
        output_mimetype=(
            cell_notif.output.mimetype
            if cell_notif.output is not None
            else None
        ),
        stdout=stdout,
        stderr=stderr,
        errors=errors,
    )


async def run_scratchpad_code(
    session: Session,
    request: Request,
    *,
    code: str,
    server_url: str,
    auth_token: str,
    timeout: float | None = EXECUTION_TIMEOUT,
) -> CodeExecutionResult:
    """Drive the kernel scratchpad on behalf of code-mode"""
    http_req = build_scratchpad_request(
        session, request, server_url=server_url, auth_token=auth_token
    )

    session.instantiate(
        InstantiateNotebookRequest(object_ids=[], values=[], auto_run=False),
        http_request=http_req,
    )

    run_id = str(uuid4())
    listener = ScratchCellListener(run_id=run_id)

    # Ensure we take a lock on the scratchpad before scoping the listener.
    # See #10035.
    async with session.scratchpad_lock:
        with session.scoped(listener):
            notebook_cells, cell_outputs = snapshot_for_scratchpad(session)
            session.put_control_request(
                ExecuteScratchpadCommand(
                    code=code,
                    request=http_req,
                    notebook_cells=notebook_cells,
                    cell_outputs=cell_outputs,
                    run_id=run_id,
                ),
                from_consumer_id=None,
            )
            settled = False
            try:
                await listener.wait(timeout=timeout)
                settled = not listener.timed_out
            finally:
                if not settled:
                    session.try_interrupt()
            if listener.timed_out:
                return CodeExecutionResult(
                    success=False,
                    errors=[f"Execution timed out after {timeout}s"],
                )

        return extract_result(session, listener)
