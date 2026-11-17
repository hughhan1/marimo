# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from inline_snapshot import snapshot

from marimo._ai._tools.types import CodeExecutionResult
from marimo._code_mode.screenshot_meta import (
    SCREENSHOT_AUTH_TOKEN_KEY,
    SCREENSHOT_FILE_KEY,
    SCREENSHOT_SERVER_URL_KEY,
)
from marimo._messaging.cell_output import CellChannel, CellOutput
from marimo._messaging.errors import MarimoExceptionRaisedError
from marimo._messaging.notification import (
    CellNotification,
    CompletedRunNotification,
    NotificationMessage,
)
from marimo._messaging.serde import serialize_kernel_message
from marimo._runtime.commands import (
    CommandMessage,
    ExecuteScratchpadCommand,
    HTTPRequest,
)
from marimo._runtime.scratch import SCRATCH_CELL_ID
from marimo._server.scratchpad import (
    ScratchCellListener,
    extract_result,
    run_scratchpad_code,
    snapshot_for_scratchpad,
)
from marimo._session.requests import InstantiateNotebookRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import Scope

    from marimo._session.session import Session
    from marimo._types.ids import CellId_t

_TEST_RUN_ID = "test-run-id"


def _completed_run(run_id: str = _TEST_RUN_ID) -> CompletedRunNotification:
    return CompletedRunNotification(run_id=run_id)


def _make_session(
    notification: CellNotification | None = None,
) -> MagicMock:
    session = MagicMock()
    session.session_view.cell_notifications = {}
    if notification is not None:
        session.session_view.cell_notifications[SCRATCH_CELL_ID] = notification
    return session


class TestSnapshotForScratchpad:
    """Snapshot helper shared by `/api/execute` and MCP `execute_code`."""

    def test_packages_document_and_outputs(self) -> None:
        from marimo._ast.cell import CellConfig
        from marimo._messaging.notebook.document import (
            NotebookCell as DocCell,
        )
        from marimo._types.ids import CellId_t

        cell = DocCell(
            id=CellId_t("a"),
            code="print('hi'); x = 1",
            name="",
            config=CellConfig(),
        )
        output = CellOutput(
            channel=CellChannel.OUTPUT, mimetype="text/plain", data="1"
        )
        console = [CellOutput.stdout("hi\n")]

        session = MagicMock()
        session.document.cells = (cell,)
        session.document.cell_ids = (cell.id,)
        session.session_view.get_cell_outputs.return_value = {cell.id: output}
        session.session_view.get_cell_console_outputs.return_value = {
            cell.id: console
        }

        notebook_cells, cell_outputs = snapshot_for_scratchpad(session)
        assert notebook_cells == (cell,)
        assert cell_outputs.output == snapshot({"a": output})
        assert cell_outputs.console_outputs == snapshot({"a": console})


def _build_request(
    *,
    scheme: str = "http",
    host: str = "localhost",
    port: int = 1234,
):
    """Build a real Starlette Request so `HTTPRequest.from_request`
    can read it without an exhaustive MagicMock setup."""
    from starlette.requests import Request

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "scheme": scheme,
        "server": (host, port),
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"host", f"{host}:{port}".encode())],
    }
    return Request(scope)


class _FakeSession:
    """Minimal duck-typed Session for `run_scratchpad_code` tests.

    Behaves like a real Session in the ways the runner cares about:

    * `scoped` registers a listener for the duration of the context.
    * `put_control_request` routes a `CompletedRunNotification` to
      the active listener so `listener.wait()` returns naturally —
      no need to monkey-patch listener internals.

    Set `auto_complete=False` to drive the timeout path (the listener
    will never see a completion event).
    """

    cell_outputs: dict[CellId_t, CellOutput]
    console_outputs: dict[CellId_t, list[CellOutput]]
    document: SimpleNamespace
    session_view: SimpleNamespace
    scratchpad_lock: asyncio.Lock
    control_requests: list[CommandMessage]
    instantiate_calls: list[
        tuple[InstantiateNotebookRequest, HTTPRequest | None]
    ]
    interrupt_count: int
    _auto_complete: bool
    _active_listener: ScratchCellListener | None
    _pre_complete_notifs: list[NotificationMessage]

    def __init__(self, *, auto_complete: bool = True) -> None:
        self.app_file_manager = SimpleNamespace(path=None)
        self.initialization_id = "__new__notebook"
        self.cell_outputs = {}
        self.console_outputs = {}
        self.document = SimpleNamespace(cells=(), cell_ids=())
        self.session_view = SimpleNamespace(
            cell_notifications={},
            get_cell_outputs=lambda _ids: self.cell_outputs,
            get_cell_console_outputs=lambda _ids: self.console_outputs,
        )
        self.scratchpad_lock = asyncio.Lock()
        self.control_requests = []
        self.instantiate_calls = []
        self.interrupt_count = 0
        self._auto_complete = auto_complete
        self._active_listener = None
        self._pre_complete_notifs = []

    def as_session(self) -> Session:
        """Type-only cast to `Session` for the runner's signature."""
        return cast("Session", cast(object, self))

    def emit(self, notification: NotificationMessage) -> None:
        """Schedule a notification to be delivered to the active
        listener just before the auto-generated completion event."""
        self._pre_complete_notifs.append(notification)

    @contextmanager
    def scoped(
        self, listener: ScratchCellListener
    ) -> Iterator[ScratchCellListener]:
        self._active_listener = listener
        try:
            yield listener
        finally:
            self._active_listener = None

    def instantiate(
        self,
        request: InstantiateNotebookRequest,
        *,
        http_request: HTTPRequest | None,
    ) -> None:
        self.instantiate_calls.append((request, http_request))

    def put_control_request(
        self,
        req: CommandMessage,
        from_consumer_id: object = None,
    ) -> None:
        del from_consumer_id
        self.control_requests.append(req)
        if not (
            self._auto_complete
            and isinstance(req, ExecuteScratchpadCommand)
            and self._active_listener is not None
        ):
            return
        session = self.as_session()
        for notif in self._pre_complete_notifs:
            self._active_listener.on_notification_sent(
                session, serialize_kernel_message(notif)
            )
        self._active_listener.on_notification_sent(
            session,
            serialize_kernel_message(
                CompletedRunNotification(run_id=req.run_id)
            ),
        )

    def try_interrupt(self) -> None:
        self.interrupt_count += 1


class TestExtractResult:
    def test_no_notification(self) -> None:
        result = extract_result(_make_session())
        assert result == CodeExecutionResult(success=True)

    def test_string_output(self) -> None:
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=CellOutput(
                channel=CellChannel.OUTPUT,
                mimetype="text/plain",
                data="hello",
            ),
            console=None,
            status="idle",
        )
        result = extract_result(_make_session(notif))
        assert result.success is True
        assert result.output == "hello"

    def test_dict_output_text_plain(self) -> None:
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=CellOutput(
                channel=CellChannel.OUTPUT,
                mimetype="text/plain",
                data={"text/plain": "value", "text/html": "<b>value</b>"},
            ),
            console=None,
            status="idle",
        )
        result = extract_result(_make_session(notif))
        assert result.output == "value"

    def test_dict_output_html_fallback(self) -> None:
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=CellOutput(
                channel=CellChannel.OUTPUT,
                mimetype="text/html",
                data={"text/html": "<b>hi</b>"},
            ),
            console=None,
            status="idle",
        )
        result = extract_result(_make_session(notif))
        assert result.output == "<b>hi</b>"

    def test_stdout_stderr(self) -> None:
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=None,
            console=[
                CellOutput(
                    channel=CellChannel.STDOUT,
                    mimetype="text/plain",
                    data="out1",
                ),
                CellOutput(
                    channel=CellChannel.STDERR,
                    mimetype="text/plain",
                    data="err1",
                ),
                CellOutput(
                    channel=CellChannel.STDOUT,
                    mimetype="text/plain",
                    data="out2",
                ),
            ],
            status="idle",
        )
        result = extract_result(_make_session(notif))
        assert result.stdout == ["out1", "out2"]
        assert result.stderr == ["err1"]

    def test_listener_stderr_preserves_cross_cell_order(self) -> None:
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=None,
            console=[
                CellOutput.stderr("scratch before\n"),
                CellOutput.stderr("scratch after\n"),
            ],
            status="idle",
        )
        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        listener.stderr.extend(
            ["scratch before\n", "child\n", "scratch after\n"]
        )

        result = extract_result(_make_session(notif), listener)

        assert result.stderr == [
            "scratch before\n",
            "child\n",
            "scratch after\n",
        ]

    def test_errors(self) -> None:
        err_obj = MagicMock()
        err_obj.msg = "NameError: x is not defined"
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=CellOutput(
                channel=CellChannel.MARIMO_ERROR,
                mimetype="application/vnd.marimo+error",
                data=[err_obj],
            ),
            console=None,
            status="idle",
        )
        result = extract_result(_make_session(notif))
        assert result.success is False
        assert len(result.errors) == 1
        assert "NameError" in result.errors[0]

    def test_child_cell_errors_included(self) -> None:
        """extract_result reports failure when listener saw child errors."""
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=CellOutput(
                channel=CellChannel.OUTPUT,
                mimetype="text/plain",
                data="summary",
            ),
            console=None,
            status="idle",
        )
        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        listener.child_error_summaries.append(
            "cell 'abc12345' raised ZeroDivisionError"
        )
        result = extract_result(_make_session(notif), listener)
        assert result.success is False
        assert result.errors == ["cell 'abc12345' raised ZeroDivisionError"]

    def test_none_console_entries_skipped(self) -> None:
        notif = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            output=None,
            console=[
                None,
                CellOutput(  # type: ignore[list-item]
                    channel=CellChannel.STDOUT,
                    mimetype="text/plain",
                    data="ok",
                ),
            ],
            status="idle",
        )
        result = extract_result(_make_session(notif))
        assert result.stdout == ["ok"]


class TestScratchCellListener:
    @pytest.mark.asyncio
    async def test_collects_scratch_console(self) -> None:
        from marimo._messaging.serde import serialize_kernel_message

        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        event_bus = MagicMock()
        session = MagicMock()
        listener.on_attach(session, event_bus)

        running = CellNotification(cell_id=SCRATCH_CELL_ID, status="running")
        console = CellNotification(
            cell_id=SCRATCH_CELL_ID,
            console=CellOutput.stdout("hello\n"),
        )
        idle = CellNotification(cell_id=SCRATCH_CELL_ID, status="idle")

        for notif in [running, console, idle, _completed_run()]:
            listener.on_notification_sent(
                session, serialize_kernel_message(notif)
            )

        await listener.wait()

        assert listener.stdout == ["hello\n"]
        assert not listener.timed_out

    @pytest.mark.asyncio
    async def test_ignores_unrelated_completed_run(self) -> None:
        """CompletedRun with a different run_id must NOT fire the sentinel."""

        from marimo._messaging.serde import serialize_kernel_message

        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        event_bus = MagicMock()
        session = MagicMock()
        listener.on_attach(session, event_bus)

        # A CompletedRun from an unrelated command (e.g. session.instantiate)
        listener.on_notification_sent(
            session, serialize_kernel_message(_completed_run("other-run-id"))
        )

        await listener.wait(timeout=0.1)

        assert listener.timed_out

    @pytest.mark.asyncio
    async def test_ignores_other_cell_status(self) -> None:
        """Status-only notifications from non-scratch cells are ignored."""
        from marimo._messaging.serde import serialize_kernel_message

        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        event_bus = MagicMock()
        session = MagicMock()
        listener.on_attach(session, event_bus)

        other_cell = CellNotification(
            cell_id="other_cell_id",
            status="running",  # type: ignore[arg-type]
        )
        listener.on_notification_sent(
            session, serialize_kernel_message(other_cell)
        )
        assert listener.stdout == []
        assert listener.stderr == []

    @pytest.mark.asyncio
    async def test_captures_other_cell_console(self) -> None:
        """Console output from non-scratch cells is captured."""
        from marimo._messaging.serde import serialize_kernel_message

        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        event_bus = MagicMock()
        session = MagicMock()
        listener.on_attach(session, event_bus)

        listener.on_notification_sent(
            session,
            serialize_kernel_message(
                CellNotification(cell_id=SCRATCH_CELL_ID, status="running")
            ),
        )
        other_console = CellNotification(
            cell_id="other_cell_id",
            console=CellOutput.stderr("error trace\n"),
        )
        listener.on_notification_sent(
            session, serialize_kernel_message(other_console)
        )
        listener.on_notification_sent(
            session, serialize_kernel_message(_completed_run())
        )

        await listener.wait()

        assert listener.stderr == ["error trace\n"]

    @pytest.mark.asyncio
    async def test_captures_stderr_in_emission_order(self) -> None:
        """Scratch and child stderr retain their notification order."""
        from marimo._messaging.serde import serialize_kernel_message

        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        session = MagicMock()

        notifications = [
            CellNotification(
                cell_id=SCRATCH_CELL_ID,
                console=CellOutput.stderr("scratch before\n"),
            ),
            CellNotification(
                cell_id="other_cell_id",
                console=[
                    CellOutput.stderr("first child error\n"),
                    CellOutput.stdout("ordinary output\n"),
                    CellOutput.stderr("second child error\n"),
                ],
            ),
            CellNotification(
                cell_id=SCRATCH_CELL_ID,
                console=CellOutput.stderr("scratch after\n"),
            ),
        ]
        for notification in notifications:
            listener.on_notification_sent(
                session, serialize_kernel_message(notification)
            )

        assert listener.stderr == [
            "scratch before\n",
            "first child error\n",
            "second child error\n",
            "scratch after\n",
        ]

    @pytest.mark.asyncio
    async def test_keeps_stderr_from_the_scratch_run_onward(self) -> None:
        """Stderr before the scratch run is ignored; trailing stderr is kept.

        Console output is flushed every 10ms, so a run's last lines can
        arrive after its completion.
        """
        from marimo._messaging.serde import serialize_kernel_message

        listener = ScratchCellListener(run_id=_TEST_RUN_ID)
        session = MagicMock()

        notifications: list[NotificationMessage] = [
            CellNotification(
                cell_id="browser-cell",
                console=CellOutput.stderr("before\n"),
            ),
            CellNotification(cell_id=SCRATCH_CELL_ID, status="running"),
            CellNotification(
                cell_id="child-cell",
                console=CellOutput.stderr("during\n"),
            ),
            _completed_run(),
            CellNotification(
                cell_id="child-cell",
                console=CellOutput.stderr("trailing\n"),
            ),
        ]
        for notification in notifications:
            listener.on_notification_sent(
                session, serialize_kernel_message(notification)
            )

        assert listener.stderr == ["during\n", "trailing\n"]


class TestRunScratchpadCode:
    """Regression guards for `run_scratchpad_code` — the runner that
    backs the AI `execute_code` tool."""

    @staticmethod
    def _execute_command(session: _FakeSession) -> ExecuteScratchpadCommand:
        cmds = [
            c
            for c in session.control_requests
            if isinstance(c, ExecuteScratchpadCommand)
        ]
        assert len(cmds) == 1, (
            f"expected one ExecuteScratchpadCommand, got {session.control_requests!r}"
        )
        return cmds[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [None, "notebooks/my notebook.py"])
    async def test_stamps_screenshot_meta_and_run_id_on_command(
        self, path: str | None
    ) -> None:
        """Regression guard: `run_id` and screenshot meta must reach
        the `ExecuteScratchpadCommand` unchanged. Without `run_id`,
        `ScratchCellListener` filters out the completion event and
        every code-mode tool call hangs ~30s before timing out."""
        session = _FakeSession()
        session.app_file_manager.path = path

        result = await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="x = 1",
            server_url="http://localhost:1234",
            auth_token="fake-token",
        )

        assert result.success is True
        cmd = self._execute_command(session)
        assert cmd.run_id is not None
        assert cmd.request is not None
        assert cmd.request.meta[SCREENSHOT_SERVER_URL_KEY] == (
            "http://localhost:1234"
        )
        assert cmd.request.meta[SCREENSHOT_AUTH_TOKEN_KEY] == "fake-token"
        assert cmd.request.meta[SCREENSHOT_FILE_KEY] == (
            path or session.initialization_id
        )

    @pytest.mark.asyncio
    async def test_snapshots_cell_outputs_onto_command(self) -> None:
        """`run_scratchpad_code` must snapshot existing cell outputs
        onto the command (via `snapshot_for_scratchpad`), same as the
        `/execute` endpoint and MCP code server. Without this, code-mode
        invoked by the AI `execute_code` tool sees every `cell.output`
        as `None` while the other entry points expose them."""
        from marimo._types.ids import CellId_t

        session = _FakeSession()
        output = CellOutput(
            channel=CellChannel.OUTPUT, mimetype="text/plain", data="42"
        )
        console = CellOutput.stdout("hi\n")
        session.cell_outputs = {CellId_t("a"): output}
        session.console_outputs = {CellId_t("a"): [console]}

        await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="x = 1",
            server_url="u",
            auth_token="t",
        )

        cmd = self._execute_command(session)
        assert cmd.cell_outputs is not None
        assert cmd.cell_outputs.output == snapshot({"a": output})
        assert cmd.cell_outputs.console_outputs == snapshot({"a": [console]})

    @pytest.mark.asyncio
    async def test_instantiates_session_without_auto_run(self) -> None:
        """The runner must seed the dependency graph before executing
        (so `_code_mode.run_cell` can resolve cell IDs) but it must
        NOT auto-run the notebook's cells."""
        session = _FakeSession()

        await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="x = 1",
            server_url="u",
            auth_token="t",
        )

        assert len(session.instantiate_calls) == 1
        instantiate_req, _ = session.instantiate_calls[0]
        assert instantiate_req.auto_run is False

    @pytest.mark.asyncio
    async def test_holds_scratchpad_lock_while_putting_execute_command(
        self,
    ) -> None:
        """`scratchpad_lock` must be held while putting the execute
        command so two concurrent code-mode calls can't interleave."""
        session = _FakeSession()
        lock_held: list[bool] = []
        original_put = session.put_control_request

        def spy(req: CommandMessage, from_consumer_id: object = None) -> None:
            if isinstance(req, ExecuteScratchpadCommand):
                lock_held.append(session.scratchpad_lock.locked())
            original_put(req, from_consumer_id)

        session.put_control_request = spy  # type: ignore[method-assign]

        await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="x = 1",
            server_url="u",
            auth_token="t",
        )

        assert lock_held == [True]

    @pytest.mark.asyncio
    async def test_timeout_interrupts_kernel_and_surfaces_in_errors(
        self,
    ) -> None:
        """On timeout the kernel is still running the (likely hung)
        scratchpad code; `run_scratchpad_code` must interrupt it so the
        next code-mode call doesn't block on `scratchpad_lock` — and
        the timeout must be reported in `errors` (plural), matching
        the success path's shape."""
        session = _FakeSession(auto_complete=False)

        result = await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="while True: pass",
            server_url="u",
            auth_token="t",
            timeout=0.05,
        )

        assert result == snapshot(
            CodeExecutionResult(
                success=False,
                errors=["Execution timed out after 0.05s"],
            )
        )
        assert session.interrupt_count == 1

    @pytest.mark.asyncio
    async def test_cancelled_wait_interrupts_kernel(self) -> None:
        """If the caller cancels mid-tool-call (e.g. the chat session
        ends or pydantic_ai aborts the run), the kernel is still
        processing our `ExecuteScratchpadCommand`. We must interrupt
        before releasing `scratchpad_lock` so the next code-mode
        call isn't blocked behind the abandoned code, and we must
        re-raise the cancellation so the caller still observes it."""
        session = _FakeSession(auto_complete=False)

        async def run() -> None:
            await run_scratchpad_code(
                session.as_session(),
                _build_request(),
                code="while True: pass",
                server_url="u",
                auth_token="t",
            )

        task = asyncio.create_task(run())
        # Yield enough for the runner to reach `await listener.wait(...)`.
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session.interrupt_count == 1

    @pytest.mark.asyncio
    async def test_timeout_interrupt_happens_while_holding_lock(
        self,
    ) -> None:
        """Regression guard: `try_interrupt()` must run BEFORE releasing
        `scratchpad_lock`. Otherwise a concurrent code-mode call could
        acquire the lock and start running between timeout detection and
        the interrupt — and get its brand-new execution killed by us."""
        session = _FakeSession(auto_complete=False)
        lock_held_during_interrupt: list[bool] = []
        original_interrupt = session.try_interrupt

        def spy() -> None:
            lock_held_during_interrupt.append(session.scratchpad_lock.locked())
            original_interrupt()

        session.try_interrupt = spy  # type: ignore[method-assign]

        await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="while True: pass",
            server_url="u",
            auth_token="t",
            timeout=0.05,
        )

        assert lock_held_during_interrupt == [True]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("message", "expected_error"),
        [
            (
                "division by zero",
                "cell 'child-cell' raised ZeroDivisionError: division by zero",
            ),
            (
                "ZeroDivisionError: division by zero",
                "cell 'child-cell' raised ZeroDivisionError: division by zero",
            ),
            (
                "ZeroDivisionError",
                "cell 'child-cell' raised ZeroDivisionError",
            ),
            ("", "cell 'child-cell' raised ZeroDivisionError"),
            (
                "This cell raised an exception: ZeroDivisionError",
                "cell 'child-cell' raised ZeroDivisionError",
            ),
        ],
    )
    async def test_child_cell_diagnostics_flow_into_result(
        self, message: str, expected_error: str
    ) -> None:
        """End-to-end: child-cell errors captured by the listener during
        execution must surface in the result with their message and traceback.
        Otherwise the AI learns only the exception type, not what failed or
        where. This also pins down the `extract_result(session, listener)`
        plumbing; dropping the listener silently loses every child-cell error.
        """
        from marimo._types.ids import CellId_t

        session = _FakeSession()
        # Real scratch cell runs emit an idle notification even when
        # their own output is empty; without one, extract_result
        # short-circuits before consulting the listener.
        session.session_view.cell_notifications[SCRATCH_CELL_ID] = (
            CellNotification(
                cell_id=SCRATCH_CELL_ID,
                output=CellOutput(
                    channel=CellChannel.OUTPUT,
                    mimetype="text/plain",
                    data="",
                ),
                console=None,
                status="idle",
            )
        )
        session.emit(
            CellNotification(cell_id=SCRATCH_CELL_ID, status="running")
        )
        session.emit(
            CellNotification(
                cell_id=CellId_t("child-cell"),
                console=CellOutput.stderr(
                    "Traceback (most recent call last):\n"
                    '  File "<cell-child-cell>", line 2, in <module>\n'
                    "    return 1 / 0\n"
                    "           ~~^~~\n"
                    "ZeroDivisionError: division by zero\n"
                ),
            )
        )
        session.emit(
            CellNotification(
                cell_id=CellId_t("child-cell"),
                output=CellOutput.errors(
                    [
                        MarimoExceptionRaisedError(
                            msg=message,
                            exception_type="ZeroDivisionError",
                            raising_cell=None,
                        )
                    ]
                ),
                console=None,
                status="idle",
            )
        )

        result = await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="run_cell('child-cell')",
            server_url="u",
            auth_token="t",
        )

        assert result.success is False
        assert result.errors == [expected_error]
        assert result.stderr == [
            (
                "Traceback (most recent call last):\n"
                '  File "<cell-child-cell>", line 2, in <module>\n'
                "    return 1 / 0\n"
                "           ~~^~~\n"
                "ZeroDivisionError: division by zero\n"
            )
        ]

    @pytest.mark.asyncio
    async def test_listener_registered_only_while_lock_held(self) -> None:
        """The listener must be scoped only while `scratchpad_lock` is held.
        Scoping before the lock lets a concurrent execute waiting on the lock
        accumulate this run's console output (see #10035)."""
        session = _FakeSession()
        locked_when_scoped: list[bool] = []
        original_scoped = session.scoped

        @contextmanager
        def recording_scoped(
            listener: ScratchCellListener,
        ) -> Iterator[ScratchCellListener]:
            locked_when_scoped.append(session.scratchpad_lock.locked())
            with original_scoped(listener) as scoped_listener:
                yield scoped_listener

        session.scoped = recording_scoped  # type: ignore[method-assign]

        await run_scratchpad_code(
            session.as_session(),
            _build_request(),
            code="x = 1",
            server_url="u",
            auth_token="t",
        )

        assert locked_when_scoped == [True]
