# Copyright 2026 Marimo. All rights reserved.
"""Host tools served by real sessions and kernels."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from marimo._core.code import code_fingerprint
from marimo._session.notebook import AppFileManager
from marimo._session.requests import InstantiateNotebookRequest
from tests._server.host.drive import call, wait_for
from tests._server.host.live import open_host

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.applications import Starlette

    from tests._server.host.drive import Response


@pytest.fixture
def notebook_source(request: pytest.FixtureRequest) -> str:
    return getattr(
        request,
        "param",
        (
            "import marimo\napp = marimo.App()\n"
            "@app.cell\ndef _():\n    example = 7\n    return (example,)\n"
        ),
    )


@pytest.fixture
async def live_host(
    tmp_path: Path,
    notebook_source: str,
) -> AsyncIterator[tuple[Starlette, str, Response]]:
    async with open_host(tmp_path, notebook_source) as host:
        yield host


def run_outcome(stream: Response, run_id: str) -> str | None:
    return next(
        (
            r["data"]["run"]["outcome"]
            for r in reversed(stream.records)
            if r.get("data", {}).get("run", {}).get("id") == run_id
            and r["data"]["run"]["status"] == "completed"
        ),
        None,
    )


async def completed(stream: Response, run_id: str) -> str:
    """Wait for the run to complete on the stream, and return its outcome."""
    await wait_for(stream, lambda s: run_outcome(s, run_id) is not None)
    outcome = run_outcome(stream, run_id)
    assert outcome is not None
    return outcome


async def execute(
    app: Starlette, notebook_id: str, key: str, code: str
) -> dict[str, Any]:
    """Execute `code` in the notebook, returning the run it was accepted as."""
    accepted = await call(
        app,
        "runtime.execute",
        idempotency_key=key,
        notebook_id=notebook_id,
        code=code,
    )
    assert accepted.ok, accepted.problem
    run: dict[str, Any] = accepted.data
    return run


async def run_cell(
    app: Starlette, notebook_id: str, key: str, cell: Any
) -> dict[str, Any]:
    """Run one cell as it was read, returning the run it was accepted as."""
    accepted = await call(
        app,
        "runtime.run",
        idempotency_key=key,
        notebook_id=notebook_id,
        cells=[{"id": cell.id, "etag": cell.etag}],
    )
    assert accepted.ok, accepted.problem
    run: dict[str, Any] = accepted.data
    return run


async def test_move_preserves_the_running_notebook(
    live_host: tuple[Starlette, str, Response], tmp_path: Path
) -> None:
    app, notebook_id, events = live_host
    host = app.state.host_context.bridge.host
    runtime = (await host.notebook(notebook_id)).runtime
    source, target = tmp_path / "notebook.py", tmp_path / "moved.py"
    session = next(iter(app.state.session_manager.sessions.values()))
    cells = list(session.app_file_manager.app.cell_manager.codes())

    moved = await call(
        app,
        "notebook.update",
        idempotency_key="move",
        notebook_id=notebook_id,
        path="moved.py",
    )
    assert moved.ok, moved.problem
    assert (moved.data["id"], moved.data["path"]) == (notebook_id, "moved.py")
    assert not source.exists()
    assert list(AppFileManager(str(target)).app.cell_manager.codes()) == cells
    assert session.app_file_manager.path == str(target)
    assert (await host.notebook(notebook_id)).runtime == runtime

    accepted = await execute(app, notebook_id, "after-move", "6 * 7")
    await completed(events, accepted["id"])
    [outcome] = [
        r["data"]["execution"]["outcome"]
        for r in events.records
        if r.get("data", {}).get("execution", {}).get("status") == "completed"
    ]
    assert outcome == {
        "kind": "value",
        "display": {
            "mimetype": "text/html",
            "data": "<pre class='text-xs'>42</pre>",
        },
    }


@pytest.mark.parametrize(
    ("code", "kind", "name", "value"),
    [
        (
            "print('hello')\nimport marimo as mo\nmo.Html('<b>9</b>')",
            "value",
            None,
            "<b>9</b>",
        ),
        ("", "value", None, None),
        ("if !!!", "error", None, None),
        ("1 / 0", "error", "ZeroDivisionError", None),
        ("input('hello')", "error", "EOFError", None),
    ],
)
async def test_scratch_outcomes_from_the_kernel(
    live_host: tuple[Starlette, str, Response],
    code: str,
    kind: str,
    name: str | None,
    value: str | None,
) -> None:
    app, notebook_id, events = live_host
    accepted = await execute(app, notebook_id, "run", code)
    await completed(events, accepted["id"])
    [execution] = [
        r["data"]["execution"]
        for r in events.records
        if r.get("data", {}).get("execution", {}).get("status") == "completed"
    ]
    outcome = execution["outcome"]
    assert execution["code"] == code
    assert execution["code_fingerprint"]
    assert execution["runtime_id"] is not None
    assert execution["caused_by"] is None
    if kind == "value":
        assert outcome == {
            "kind": "value",
            "display": None
            if value is None
            else {"mimetype": "text/html", "data": value},
        }
        if value is not None:
            assert "hello\n" == "".join(
                r["data"]["console"]["text"]
                for r in events.records
                if r.get("event") == "console"
            )
    else:
        assert (
            outcome["kind"],
            outcome["error"]["kind"],
            outcome["error"]["name"],
        ) == (
            "error",
            "invalid-syntax" if name is None else "exception",
            name,
        )
        assert outcome["error"]["message"]


async def test_cell_runs_preserve_order_with_later_deletions(
    live_host: tuple[Starlette, str, Response],
) -> None:
    app, notebook_id, events = live_host
    notebook = await app.state.host_context.bridge.host.notebook(notebook_id)
    cell = next(iter(notebook.document))
    session = next(iter(app.state.session_manager.sessions.values()))

    await completed(
        events, (await run_cell(app, notebook_id, "warm", cell))["id"]
    )
    # A scratch client must not let a later edit overtake an accepted cell run.
    async with session.scratchpad_lock:
        accepted = await run_cell(app, notebook_id, "run", cell)
        edited = await call(
            app,
            "notebook.edit",
            idempotency_key="edit",
            notebook_id=notebook_id,
            changes=[{"kind": "delete_cell", "id": cell.id, "base_etag": "*"}],
        )
        assert edited.ok, edited.problem
    await completed(events, accepted["id"])

    checked = await execute(
        app,
        notebook_id,
        "check-graph",
        "from marimo._runtime.context import get_context as _ctx\n"
        f"assert {cell.id!r} not in _ctx().graph.cells",
    )
    assert await completed(events, checked["id"]) == "succeeded"
    assert notebook.document.cell(cell.id) is None


@pytest.fixture
async def gated_host(
    tmp_path: Path,
) -> AsyncIterator[tuple[Starlette, str, Response, Path]]:
    """A notebook whose first cell runs until the test creates `gate`."""
    gate = tmp_path / "gate"
    source = (
        "import marimo\napp = marimo.App()\n"
        "@app.cell\ndef _():\n"
        "    import pathlib, time\n"
        "    for _ in range(1000):\n"
        f"        if pathlib.Path({str(gate)!r}).exists():\n"
        "            break\n"
        "        time.sleep(0.01)\n"
        "    slow = 1\n    return (slow,)\n"
        "@app.cell\ndef _():\n    quick = 2\n    return (quick,)\n"
    )
    async with open_host(tmp_path, source) as (app, notebook_id, events):
        yield app, notebook_id, events, gate


def run_transitions(stream: Response, *run_ids: str) -> list[tuple[str, str]]:
    return [
        (r["data"]["run"]["id"], r["data"]["run"]["status"])
        for r in stream.records
        if r.get("data", {}).get("run", {}).get("id") in run_ids
    ]


async def test_runs_accepted_while_busy_start_in_order(
    gated_host: tuple[Starlette, str, Response, Path],
) -> None:
    app, notebook_id, events, gate = gated_host
    notebook = await app.state.host_context.bridge.host.notebook(notebook_id)
    slow, quick = notebook.document

    first_id = (await run_cell(app, notebook_id, "first", slow))["id"]
    await wait_for(
        events, lambda s: (first_id, "running") in run_transitions(s, first_id)
    )

    second = await run_cell(app, notebook_id, "second", quick)
    second_id = second["id"]
    assert second["status"] == "queued"
    assert second["started_at"] is None
    await wait_for(
        events,
        lambda s: (second_id, "queued") in run_transitions(s, second_id),
    )
    assert run_outcome(events, first_id) is None

    gate.touch()
    await wait_for(events, lambda s: run_outcome(s, second_id) is not None)
    seen = run_transitions(events, first_id, second_id)
    assert seen.index((first_id, "completed")) < seen.index(
        (second_id, "running")
    )
    assert run_outcome(events, first_id) == "succeeded"
    assert run_outcome(events, second_id) == "succeeded"


async def test_interrupting_a_queued_run_cancels_it_before_it_starts(
    gated_host: tuple[Starlette, str, Response, Path],
) -> None:
    app, notebook_id, events, gate = gated_host
    notebook = await app.state.host_context.bridge.host.notebook(notebook_id)
    slow, quick = notebook.document

    first_id = (await run_cell(app, notebook_id, "first", slow))["id"]
    await wait_for(
        events, lambda s: (first_id, "running") in run_transitions(s, first_id)
    )
    queued_id = (await run_cell(app, notebook_id, "queued", quick))["id"]
    await wait_for(
        events,
        lambda s: (queued_id, "queued") in run_transitions(s, queued_id),
    )

    cancelled = await call(
        app,
        "runtime.cancel",
        idempotency_key="cancel",
        notebook_id=notebook_id,
        run_id=queued_id,
    )
    assert cancelled.ok, cancelled.problem
    assert run_outcome(events, first_id) is None

    # The kernel reaches the cancellation once the first run lets it go.
    gate.touch()
    assert await completed(events, queued_id) == "interrupted"
    assert run_outcome(events, first_id) == "succeeded"
    assert not any(
        r.get("data", {}).get("execution", {}).get("run_id") == queued_id
        for r in events.records
    )

    checked = await execute(
        app, notebook_id, "check", "assert 'quick' not in globals()"
    )
    assert await completed(events, checked["id"]) == "succeeded"


@pytest.mark.parametrize(
    "notebook_source",
    [
        (
            "import marimo\napp = marimo.App()\n"
            "@app.cell\ndef _():\n    answer = input('prompt')\n    return (answer,)\n"
        )
    ],
    indirect=True,
)
async def test_host_cell_input_fails_without_waiting_for_stdin(
    live_host: tuple[Starlette, str, Response],
) -> None:
    app, notebook_id, events = live_host
    notebook = await app.state.host_context.bridge.host.notebook(notebook_id)
    cell = next(iter(notebook.document))
    accepted = await run_cell(app, notebook_id, "run", cell)
    await completed(events, accepted["id"])
    [execution] = [
        r["data"]["execution"]
        for r in events.records
        if r.get("data", {}).get("execution", {}).get("status") == "completed"
    ]
    assert execution["outcome"]["error"]["name"] == "EOFError"
    assert notebook.executions.active() is None


async def test_background_output_after_a_scratch_run_is_dropped(
    live_host: tuple[Starlette, str, Response],
) -> None:
    """A completed scratch run is history; the backend forgets it.

    Output its code produces later, from a background thread, has no
    current execution to join and never reaches clients.
    """
    app, notebook_id, events = live_host
    code = """import builtins
import threading
import marimo as mo
builtins._host_test_gate = threading.Event()
builtins._host_test_printed = threading.Event()
def worker():
    builtins._host_test_gate.wait(5)
    print("late output")
    builtins._host_test_printed.set()
mo.Thread(target=worker).start()
"""
    first = await execute(app, notebook_id, "first", code)
    await completed(events, first["id"])
    second = await execute(
        app,
        notebook_id,
        "second",
        "import builtins\nbuiltins._host_test_gate.set()\nassert builtins._host_test_printed.wait(5)\ndel builtins._host_test_gate, builtins._host_test_printed",
    )
    assert await completed(events, second["id"]) == "succeeded"
    console = [
        r["data"]["console"]
        for r in events.records
        if r.get("event") == "console"
    ]
    assert not any("late output" in item["text"] for item in console)


async def test_queued_cancellation_never_executes_the_code(
    live_host: tuple[Starlette, str, Response], tmp_path: Path
) -> None:
    app, notebook_id, events = live_host
    session = next(iter(app.state.session_manager.sessions.values()))
    marker = tmp_path / "executed"
    async with session.scratchpad_lock:
        accepted = await execute(
            app,
            notebook_id,
            "canceled",
            f"__import__('pathlib').Path({str(marker)!r}).touch()",
        )
        cancelled = await call(
            app,
            "runtime.cancel",
            idempotency_key="interrupt",
            notebook_id=notebook_id,
            run_id=accepted["id"],
        )
        assert cancelled.ok, cancelled.problem
        await completed(events, accepted["id"])
    [outcome] = [
        r["data"]["execution"]["outcome"]
        for r in events.records
        if r.get("data", {}).get("execution", {}).get("status") == "completed"
    ]
    assert outcome == {"kind": "interrupted"}
    following = await execute(app, notebook_id, "next", "42")
    await completed(events, following["id"])
    assert not marker.exists()


async def test_run_retries_return_the_result_even_after_restart(
    live_host: tuple[Starlette, str, Response], tmp_path: Path
) -> None:
    app, notebook_id, events = live_host
    marker = tmp_path / "executions"
    code = (
        f"_marker = __import__('pathlib').Path({str(marker)!r})\n"
        "_marker.write_text((_marker.read_text() if _marker.exists() else '') + 'x')"
    )
    accepted = await execute(app, notebook_id, "once", code)
    await completed(events, accepted["id"])
    [done] = [
        r["data"]["run"]
        for r in events.records
        if r.get("data", {}).get("run", {}).get("status") == "completed"
    ]
    assert await execute(app, notebook_id, "once", code) == done

    restarted = await call(
        app,
        "runtime.restart",
        idempotency_key="restart",
        notebook_id=notebook_id,
    )
    assert restarted.ok, restarted.problem
    new_runtime_id = restarted.data["id"]
    assert restarted.data["status"] == "starting"
    await wait_for(
        events,
        lambda response: any(
            r.get("event") == "runtime"
            and r["data"]["runtime"]["id"] == new_runtime_id
            and r["data"]["runtime"]["status"] == "running"
            for r in response.records
        ),
        timeout=15,
    )
    assert await execute(app, notebook_id, "once", code) == done
    assert marker.read_text() == "x"


CASCADE_NOTEBOOK = """import marimo
app = marimo.App()

@app.cell
def _():
    import anywidget
    import traitlets
    import threading
    import marimo as mo
    class CounterWidget(anywidget.AnyWidget):
        value = traitlets.Int(0).tag(sync=True)
    counter = CounterWidget()
    get_x, set_x = mo.state(counter.value)
    counter.observe(lambda _: set_x(counter.value), names="value")
    gate = threading.Event()
    return counter, get_x, set_x, gate, mo

@app.cell
def _(mo):
    get_y, set_y = mo.state(0)
    return get_y, set_y

@app.cell
def _(get_x, set_y):
    current = get_x()
    if current == 99:
        raise ValueError("downstream failed")
    set_y(current)
    return

@app.cell
def _(get_y):
    print(10 + get_y())
    return
"""


async def initialize_cascade(app: Starlette, events: Response) -> None:
    # Pytest can import the test directory as an empty namespace package.
    if not hasattr(pytest.importorskip("anywidget"), "AnyWidget"):
        pytest.skip("requires anywidget")
    session = next(iter(app.state.session_manager.sessions.values()))
    session.instantiate(
        InstantiateNotebookRequest(object_ids=[], values=[], auto_run=True),
        http_request=None,
    )
    await wait_for(
        events,
        lambda response: any(
            r.get("data", {}).get("run", {}).get("status") == "completed"
            for r in response.records
        ),
        # Initial widget imports and construction can be slow on CI.
        timeout=15,
    )
    runs = [
        r["data"]["run"] for r in events.records if r.get("event") == "run"
    ]
    assert runs[-1]["outcome"] == "succeeded", [
        r["data"]["execution"]["outcome"]
        for r in events.records
        if r.get("event") == "execution"
        and r["data"]["execution"]["status"] == "completed"
    ]


@pytest.mark.requires("anywidget", "traitlets")
@pytest.mark.parametrize(
    "notebook_source", [CASCADE_NOTEBOOK], indirect=True, ids=["widget"]
)
@pytest.mark.parametrize(
    ("value", "outcome", "children"), [(20, "succeeded", 2), (99, "failed", 1)]
)
async def test_scratch_run_includes_the_full_reactive_cycle(
    live_host: tuple[Starlette, str, Response],
    value: int,
    outcome: str,
    children: int,
) -> None:
    app, notebook_id, events = live_host
    await initialize_cascade(app, events)
    accepted = await execute(
        app, notebook_id, "cascade", f"counter.value = {value}"
    )
    await completed(events, accepted["id"])
    records = [
        r
        for r in events.records
        if r.get("data", {}).get("request_id") == "cascade"
    ]
    assert records[-1]["event"] == "run"
    assert records[-1]["data"]["run"]["outcome"] == outcome
    executions = [
        r["data"]["execution"]
        for r in records
        if r["event"] == "execution"
        and r["data"]["execution"]["status"] == "completed"
    ]
    assert len(executions) == children + 1
    root, *downstream = executions
    assert root["cell_id"] is None
    assert root["outcome"]["kind"] == "value"
    assert all(
        e["run_id"] == accepted["id"]
        and e["cell_id"] is not None
        and e["caused_by"] == root["id"]
        for e in downstream
    )
    assert downstream[-1]["outcome"]["kind"] == (
        "error" if outcome == "failed" else "value"
    )
    if outcome == "succeeded":
        consoles = [
            r["data"]["console"] for r in records if r["event"] == "console"
        ]
        assert "".join(c["text"] for c in consoles) == "30\n"
        assert {c["execution_id"] for c in consoles} == {downstream[-1]["id"]}


@pytest.mark.requires("anywidget", "traitlets")
@pytest.mark.parametrize(
    "notebook_source", [CASCADE_NOTEBOOK], indirect=True, ids=["widget"]
)
@pytest.mark.parametrize("command", [False, True])
async def test_detached_reactivity_starts_a_new_run(
    live_host: tuple[Starlette, str, Response],
    command: bool,
) -> None:
    app, notebook_id, events = live_host
    await initialize_cascade(app, events)
    origin = (
        "import asyncio\n"
        "import marimo._code_mode as cm\n"
        "from marimo._runtime.commands import ExecuteCellCommand, ExecuteCellsCommand\n"
        "ctx = cm.get_context()\n"
        "async def later():\n"
        "    await asyncio.to_thread(gate.wait)\n"
        "    await ctx.execute_command(ExecuteCellsCommand(execution_requests=[ExecuteCellCommand(cell_id='later', code='print(30)')]))\n"
        "asyncio.create_task(later())"
        if command
        else "mo.Thread(target=lambda: (gate.wait(), set_x(20))).start()"
    )
    submitted = {}
    for request_id, code in [
        ("origin", origin),
        ("release", "gate.set()"),
    ]:
        run_id = (await execute(app, notebook_id, request_id, code))["id"]
        submitted[request_id] = run_id
        await completed(events, run_id)
    await wait_for(
        events,
        lambda response: any(
            "30\n" in r.get("data", {}).get("console", {}).get("text", "")
            for r in response.records
        ),
    )
    printed = next(
        r["data"]["console"]
        for r in events.records
        if "30\n" in r.get("data", {}).get("console", {}).get("text", "")
    )
    execution = next(
        r["data"]["execution"]
        for r in events.records
        if r.get("data", {}).get("execution", {}).get("id")
        == printed["execution_id"]
    )
    assert execution["run_id"] not in submitted.values()
    await completed(events, execution["run_id"])
    origin_runs = [
        r["data"]["run"]
        for r in events.records
        if r.get("data", {}).get("run", {}).get("id") == submitted["origin"]
    ]
    assert [r["status"] for r in origin_runs] == [
        "queued",
        "running",
        "completed",
    ]
    assert origin_runs[-1]["outcome"] == "succeeded"


async def test_nested_evaluations_keep_the_scratch_result_and_skipped_child(
    live_host: tuple[Starlette, str, Response],
) -> None:
    app, notebook_id, events = live_host
    code = (
        "import marimo._code_mode as cm\n"
        "async with cm.get_context() as ctx:\n"
        "    a = ctx.create_cell('x = 1/0')\n"
        "    b = ctx.create_cell('y = x + 1')\n"
        "    ctx.run_cell(a)\n"
        "    ctx.run_cell(b)\n"
        "42"
    )
    accepted = await execute(app, notebook_id, "nested", code)
    assert await completed(events, accepted["id"]) == "failed"
    records = [
        r
        for r in events.records
        if r.get("data", {}).get("request_id") == "nested"
    ]
    executions = [
        r["data"]["execution"]
        for r in records
        if r["event"] == "execution"
        and r["data"]["execution"]["status"] == "completed"
    ]
    assert len(executions) == 3
    [root] = [e for e in executions if e["cell_id"] is None]
    assert root["outcome"]["kind"] == "value"
    assert "42" in root["outcome"]["display"]["data"]
    children = [e for e in executions if e["cell_id"] is not None]
    assert all(e["caused_by"] == root["id"] for e in children)
    assert {e["code_fingerprint"]: e["outcome"]["kind"] for e in children} == {
        code_fingerprint("x = 1/0"): "error",
        code_fingerprint("y = x + 1"): "skipped",
    }


async def test_a_fixed_cell_replaces_its_failed_compilation(
    live_host: tuple[Starlette, str, Response],
) -> None:
    app, notebook_id, events = live_host
    code = (
        "import marimo._code_mode as cm\n"
        "from marimo._runtime.commands import ExecuteCellCommand, ExecuteCellsCommand\n"
        "await cm.get_context().execute_command(ExecuteCellsCommand(execution_requests=[ExecuteCellCommand(cell_id='child', code='if !!!')]))\n"
        "await cm.get_context().execute_command(ExecuteCellsCommand(execution_requests=[ExecuteCellCommand(cell_id='child', code='x = 42')]))\n"
        "42"
    )
    accepted = await execute(app, notebook_id, "retry", code)
    assert await completed(events, accepted["id"]) == "failed"
    executions = [
        r["data"]["execution"]
        for r in events.records
        if r.get("data", {}).get("execution", {}).get("cell_id") == "child"
        and r["data"]["execution"]["status"] == "completed"
    ]
    assert [
        (e["code_fingerprint"], e["outcome"]["kind"]) for e in executions
    ] == [
        (code_fingerprint("if !!!"), "error"),
        (code_fingerprint("x = 42"), "value"),
    ]
    assert len({e["id"] for e in executions}) == 2


async def test_nested_commands_can_run_a_cell_queued_by_the_outer_command(
    live_host: tuple[Starlette, str, Response],
) -> None:
    app, notebook_id, events = live_host
    inner = (
        "x = 42\n"
        "import marimo._code_mode as cm\n"
        "from marimo._runtime.commands import ExecuteCellCommand, ExecuteCellsCommand\n"
        "await cm.get_context().execute_command(ExecuteCellsCommand("
        "execution_requests=[ExecuteCellCommand(cell_id='b', code='print(x)')]))"
    )
    code = (
        "import marimo._code_mode as cm\n"
        "from marimo._runtime.commands import ExecuteCellCommand, ExecuteCellsCommand\n"
        "await cm.get_context().execute_command(ExecuteCellsCommand("
        f"execution_requests=[ExecuteCellCommand(cell_id='a', code={inner!r}), ExecuteCellCommand(cell_id='b', code='print(x)')]))"
    )
    accepted = await execute(app, notebook_id, "nested-queue", code)
    assert await completed(events, accepted["id"]) == "succeeded"
    executions = [
        r["data"]["execution"]
        for r in events.records
        if r.get("data", {}).get("execution", {}).get("cell_id") == "b"
        and r["data"]["execution"]["status"] == "completed"
    ]
    assert [e["outcome"]["kind"] for e in executions] == ["value", "value"]
    assert len({e["id"] for e in executions}) == 2


@pytest.mark.parametrize("nested_scratch", [False, True])
@pytest.mark.parametrize("child_code", ["1/0", "if !!!"])
async def test_awaited_commands_join_the_enclosing_run(
    live_host: tuple[Starlette, str, Response],
    nested_scratch: bool,
    child_code: str,
) -> None:
    app, notebook_id, events = live_host
    command = (
        f"ExecuteScratchpadCommand(code={child_code!r}, request=None)"
        if nested_scratch
        else f"ExecuteCellsCommand(execution_requests=[ExecuteCellCommand(cell_id='child', code={child_code!r})])"
    )
    sibling = (
        "await cm.get_context().execute_command(ExecuteScratchpadCommand(code='3 * 3', request=None))\n"
        if nested_scratch
        else ""
    )
    code = (
        "import marimo._code_mode as cm\n"
        "from marimo._runtime.commands import ExecuteCellCommand, ExecuteCellsCommand, ExecuteScratchpadCommand\n"
        f"await cm.get_context().execute_command({command})\n{sibling}42"
    )
    before = len(events.records)
    accepted = await execute(app, notebook_id, "awaited", code)
    assert await completed(events, accepted["id"]) == "failed"
    records = events.records[before:]
    runs = [r["data"]["run"] for r in records if r.get("event") == "run"]
    assert {r["id"] for r in runs} == {accepted["id"]}
    executions = [
        r["data"]["execution"]
        for r in records
        if r.get("event") == "execution"
        and r["data"]["execution"]["status"] == "completed"
    ]
    assert len(executions) == (3 if nested_scratch else 2)
    [root] = [e for e in executions if e["caused_by"] is None]
    children = [e for e in executions if e["caused_by"] == root["id"]]
    assert len(children) == len(executions) - 1
    child = children[0]
    assert root["outcome"]["kind"] == "value"
    assert "42" in root["outcome"]["display"]["data"]
    assert child["outcome"]["kind"] == "error"
    assert child["code_fingerprint"] == code_fingerprint(child_code)
    assert all(e["run_id"] == accepted["id"] for e in executions)


@pytest.mark.parametrize(
    "notebook_source",
    [
        "import marimo\napp = marimo.App()\n@app.cell\ndef _():\n    x = __file__\n    return (x,)\n"
    ],
    indirect=True,
)
async def test_rename_reactivity_completes_its_run(
    live_host: tuple[Starlette, str, Response],
) -> None:
    app, notebook_id, events = live_host
    session = next(iter(app.state.session_manager.sessions.values()))
    session.instantiate(
        InstantiateNotebookRequest(object_ids=[], values=[], auto_run=True),
        http_request=None,
    )
    await wait_for(
        events,
        lambda response: any(
            r.get("data", {}).get("run", {}).get("status") == "completed"
            for r in response.records
        ),
    )
    before = len(events.records)
    moved = await call(
        app,
        "notebook.update",
        idempotency_key="rename",
        notebook_id=notebook_id,
        path="renamed.py",
    )
    assert moved.ok, moved.problem
    await wait_for(
        events,
        lambda response: any(
            r.get("data", {}).get("run", {}).get("status") == "completed"
            for r in response.records[before:]
        ),
    )
    runs = [
        r["data"]["run"]
        for r in events.records[before:]
        if r.get("event") == "run"
    ]
    assert runs[-1]["outcome"] == "succeeded"
    await execute(app, notebook_id, "after-rename", "42")
