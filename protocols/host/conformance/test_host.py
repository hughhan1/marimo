# Copyright 2026 Marimo. All rights reserved.
"""Check a running host through its tools and event streams."""

from __future__ import annotations

import asyncio
from itertools import groupby
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest
from contract import Failure, document_state, failed, objects, problem


def stream(notebook):
    return f"/notebooks/{quote(notebook['id'], safe='')}/events"


def require(notebook, *operations: str):
    missing = set(operations) - set(notebook["operations"])
    if missing:
        pytest.skip(f"notebook does not offer {', '.join(sorted(missing))}")


def live(frame, key):
    assert frame.id, "live event has no cursor"
    message = frame.message()
    assert message.get("time"), "live event has no timestamp"
    assert message.get("request_id") == key, "event has the wrong request_id"
    return message


async def test_required_operations(catalog, pytestconfig):
    available = set(objects(catalog, "host")[0]["operations"])
    assert set(pytestconfig.getoption("--require-operation")) <= available


@pytest.mark.parametrize("authorization", ["", "Bearer wrong-token"])
async def test_security_token(host, authorization):
    response = await host.client.get(
        "events", headers={"Authorization": authorization}
    )
    problem(response, 401, "unauthorized")


async def test_security_cookie(host, record):
    response = await host.client.get(
        "events",
        headers={"Authorization": "", "Cookie": f"token={record['token']}"},
    )
    problem(response, 401, "unauthorized")


async def test_security_cors(host):
    response = await host.client.options(
        "events",
        headers={
            "Origin": "https://example.invalid",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert "access-control-allow-origin" not in response.headers
    async with host.watch(
        headers={"Origin": "https://example.invalid"}
    ) as watch:
        await watch.ready()


async def test_catalog_snapshot(catalog):
    events = [frame for frame in catalog if frame.comment is None]
    assert events[0].event == "host"
    assert events[-1].event == "ready"
    assert events[-1].id
    assert len(objects(catalog, "host")) == 1
    projects, notebooks = set(), set()
    for frame in events[:-1]:
        assert frame.id is None, "snapshot event has an id"
        message = frame.message()
        if frame.event == "project":
            projects.add(message["project"]["id"])
        elif frame.event == "notebook":
            notebook = message["notebook"]
            assert notebook["project_id"] in projects
            notebooks.add(notebook["id"])
        elif frame.event in ("runtime", "attachment"):
            assert message[frame.event]["notebook_id"] in notebooks


async def test_stream_types_filter(host):
    snapshot = await host.snapshot(params={"types": "runtime"})
    assert {frame.event for frame in snapshot if frame.comment is None} <= {
        "runtime",
        "ready",
    }


async def test_stream_resume(host, catalog):
    cursor = catalog[-1].id
    resumed = await host.snapshot(headers={"Last-Event-ID": cursor})
    events = [frame for frame in resumed if frame.comment is None]
    assert events[-1].id
    if events[0].event == "reset":
        assert events[0].id is None
        assert all(frame.id is None for frame in events[1:-1])
        assert events[1].event == "host"
    else:
        assert all(frame.id for frame in events), (
            "resume sent a snapshot without reset"
        )


async def test_stream_ping(host, pytestconfig):
    if not pytestconfig.getoption("--slow"):
        pytest.skip("pass --slow to wait for idle pings")
    # Filter out attachments caused by other stream checks.
    async with host.watch(params={"types": "host"}, timeout=20) as watch:
        await watch.ready()
        watch.timeout = 20
        await watch.until(lambda frame: frame.comment == "ping")


async def test_notebook_document(host, catalog):
    notebooks = objects(catalog, "notebook")
    if not notebooks:
        pytest.skip("host has no notebooks")
    snapshot = await host.snapshot(stream(notebooks[0]))
    document_state(snapshot)
    assert objects(snapshot, "attachment"), "notebook stream has no attachment"
    filtered = await host.snapshot(
        stream(notebooks[0]), params={"types": "cell,document"}
    )
    assert {f.event for f in filtered if f.comment is None} <= {
        "cell",
        "document",
        "ready",
    }
    document_state(filtered)


async def test_create_duplicate_replay_and_missing_key(host, scratch):
    arguments = {key: scratch[key] for key in ("project_id", "name")}
    if scratch["path"] is not None:
        arguments["path"] = scratch["path"]
        failed(await host.call("notebook.create", **arguments), "conflict")
    failed(
        await host.call("notebook.create", idempotency_key=None, **arguments),
        "invalid-request",
    )
    # Replay a mutation of our scratch notebook, so cleanup always owns it.
    require(scratch, "notebook.update")
    rename = {"notebook_id": scratch["id"], "idempotency_key": uuid4().hex}
    first = await host.call(
        "notebook.update", name="conformance-renamed", **rename
    )
    second = await host.call(
        "notebook.update", name="conformance-renamed", **rename
    )
    assert first["name"] == "conformance-renamed"
    assert first == second
    failed(
        await host.call("notebook.update", name="different", **rename),
        "conflict",
    )


def put(cell_id, code, base_etag=None):
    return {
        "kind": "put_cell",
        "base_etag": base_etag,
        "cell": {"id": cell_id, "code": code},
    }


async def edit(host, notebook, changes, **arguments: Any):
    return await host.call(
        "notebook.edit",
        notebook_id=notebook["id"],
        changes=changes,
        **arguments,
    )


async def test_edit_atomicity_and_conflicts(host, scratch):
    require(scratch, "notebook.edit")
    key = uuid4().hex
    async with host.watch(stream(scratch)) as watch:
        await watch.ready()
        answer = await edit(
            host, scratch, [put("check_a", "x = 1")], idempotency_key=key
        )
        events = await watch.until(lambda f: f.event == "document")
        correlated = [f for f in events if f.comment is None]
        assert [f.event for f in correlated] == ["cell", "document"]
        for frame in correlated:
            live(frame, key)
        assert correlated[-1].message()["document"] == answer["document"]
        assert (
            answer["cells"]["check_a"]["etag"]
            == correlated[0].message()["cell"]["etag"]
        )
    before, cells = document_state(await host.snapshot(stream(scratch)))
    # A valid change followed by a stale one must apply neither.
    stale = failed(
        await edit(
            host,
            scratch,
            [put("check_b", "y = 2"), put("check_a", "x = 3", "stale")],
        ),
        "precondition-failed",
    )
    assert stale["index"] == 1
    assert stale["cell"] == cells["check_a"]
    assert stale["document"] == before
    after, after_cells = document_state(await host.snapshot(stream(scratch)))
    assert (after, after_cells) == (before, cells)
    failed(
        await edit(
            host,
            scratch,
            [{"kind": "set_order", "base_etag": "*", "order": []}],
        ),
        "invalid-request",
    )
    assert document_state(await host.snapshot(stream(scratch))) == (
        before,
        cells,
    )


async def test_concurrent_edits(host, scratch):
    require(scratch, "notebook.edit")
    answers = await asyncio.gather(
        edit(host, scratch, [put("check_a", "x = 1")]),
        edit(host, scratch, [put("check_b", "y = 2")]),
    )
    for answer in answers:
        assert answer["document"]
    _, cells = document_state(await host.snapshot(stream(scratch)))
    assert {"check_a", "check_b"} <= cells.keys()


async def test_save(host, scratch):
    require(scratch, "notebook.edit", "notebook.save")
    answer = await edit(host, scratch, [put("check_a", "x = 1")])
    saved = await host.call(
        "notebook.save",
        notebook_id=scratch["id"],
        expected_etag=answer["document"]["etag"],
    )
    document = saved["document"]
    assert document["saved_etag"] == document["etag"]
    stale = failed(
        await host.call(
            "notebook.save", notebook_id=scratch["id"], expected_etag="stale"
        ),
        "precondition-failed",
    )
    assert (stale["index"], stale["cell"]) == (0, None)
    assert stale["document"] == document


async def execute(host, notebook, code, **arguments: Any):
    return await host.call(
        "runtime.execute", notebook_id=notebook["id"], code=code, **arguments
    )


async def test_no_runtime(host, scratch):
    require(scratch, "runtime.execute")
    failed(await execute(host, scratch, "1"), "no-runtime")


async def start(host, notebook, watch):
    key = uuid4().hex
    runtime = await host.call(
        "runtime.start", notebook_id=notebook["id"], idempotency_key=key
    )
    if isinstance(runtime, Failure):
        failed(runtime, "decision-required")
        pytest.skip("runtime requires a user decision")
    assert runtime["status"] == "starting"
    events = await watch.until(
        lambda f: (
            f.event == "runtime"
            and f.message()["runtime"]["id"] == runtime["id"]
            and f.message()["runtime"]["status"] in ("running", "failed")
        )
    )
    assert events[-1].message()["runtime"]["status"] == "running"
    live(events[-1], key)
    return runtime


async def finish_run(watch, run, key, outcome):
    events = await watch.until(
        lambda f: (
            f.event == "run"
            and f.message()["run"]["id"] == run["id"]
            and f.message()["run"]["status"] == "completed"
        )
    )
    executions = {}
    for frame in events:
        if frame.event == "execution":
            execution = frame.message()["execution"]
            if execution["run_id"] == run["id"]:
                live(frame, key)
                executions[execution["id"]] = execution
    assert executions, "run completed without executions"
    assert all(
        e["status"] == "completed" and e["outcome"] is not None
        for e in executions.values()
    ), "run completed before its executions"
    assert live(events[-1], key)["run"]["outcome"] == outcome
    return events, list(executions.values())


@pytest.mark.parametrize(
    ("code", "outcome", "kind"),
    [
        ('print("conformance")', "succeeded", "value"),
        ("raise RuntimeError('conformance')", "failed", "error"),
    ],
)
async def test_run(host, scratch, code, outcome, kind):
    require(scratch, "runtime.start", "runtime.stop", "runtime.execute")
    async with host.watch(stream(scratch)) as watch:
        await watch.ready()
        await start(host, scratch, watch)
        key = uuid4().hex
        run = await execute(host, scratch, code, idempotency_key=key)
        assert run["status"] in ("queued", "running")
        assert run["id"] != key
        events, executions = await finish_run(watch, run, key, outcome)
        assert all(
            e["code"] == code and e["cell_id"] is None for e in executions
        )
        assert executions[-1]["outcome"]["kind"] == kind
        if kind == "value":
            assert "conformance" in "".join(
                item["text"] for item in objects(events, "console")
            )
        replay = await execute(host, scratch, code, idempotency_key=key)
        assert replay["id"] == run["id"]
        assert replay["status"] == "completed"


async def test_runtime_lifecycle(host, scratch):
    require(scratch, "runtime.start", "runtime.stop", "runtime.restart")
    async with host.watch(stream(scratch)) as watch:
        await watch.ready()
        runtime = await start(host, scratch, watch)
        existing = await host.call("runtime.start", notebook_id=scratch["id"])
        assert existing["id"] == runtime["id"]
        assert existing["status"] == "running"
        failed(
            await host.call("notebook.delete", notebook_id=scratch["id"]),
            "conflict",
        )
        key = uuid4().hex
        replacement = await host.call(
            "runtime.restart", notebook_id=scratch["id"], idempotency_key=key
        )
        assert replacement["id"] != runtime["id"]
        assert replacement["status"] == "starting"
        events = await watch.until(
            lambda f: (
                f.event == "runtime"
                and f.message()["runtime"]["id"] == replacement["id"]
                and f.message()["runtime"]["status"] in ("running", "failed")
            )
        )
        restart_events = [
            frame
            for frame in events
            if frame.event in ("runtime", "runtime.removed")
            and frame.message().get("request_id") == key
        ]
        transitions = [
            (f.event, f.message().get("runtime", {}).get("status"))
            for f in restart_events
        ]
        assert [transition for transition, _ in groupby(transitions)] == [
            ("runtime", "terminating"),
            ("runtime.removed", None),
            ("runtime", "starting"),
            ("runtime", "running"),
        ]
        for frame in restart_events:
            live(frame, key)
        key = uuid4().hex
        stopped = await host.call(
            "runtime.stop", notebook_id=scratch["id"], idempotency_key=key
        )
        assert stopped["id"] == replacement["id"]
        events = await watch.until(lambda f: f.event == "runtime.removed")
        assert live(events[-1], key)["id"] == replacement["id"]


async def test_run_cells(host, scratch):
    require(
        scratch,
        "notebook.edit",
        "runtime.start",
        "runtime.stop",
        "runtime.run",
    )
    answer = await edit(host, scratch, [put("check_a", "x = 1")])
    async with host.watch(stream(scratch)) as watch:
        await watch.ready()
        await start(host, scratch, watch)
        key = uuid4().hex
        run = await host.call(
            "runtime.run",
            notebook_id=scratch["id"],
            idempotency_key=key,
            cells=[
                {"id": "check_a", "etag": answer["cells"]["check_a"]["etag"]}
            ],
        )
        _, executions = await finish_run(watch, run, key, "succeeded")
        assert any(
            e["cell_id"] == "check_a" and e["code"] is None for e in executions
        )
        stale = failed(
            await host.call(
                "runtime.run",
                notebook_id=scratch["id"],
                cells=[{"id": "check_a", "etag": "stale"}],
            ),
            "precondition-failed",
        )
        assert stale["index"] == 0
        assert stale["cell"] is not None, "run lost the cell from the document"
        assert stale["cell"]["id"] == "check_a"
        assert stale["document"] == answer["document"]


@pytest.mark.parametrize("tool", ["runtime.interrupt", "runtime.cancel"])
async def test_interrupt_and_cancel_have_different_queue_scope(
    host, scratch, tool
):
    require(scratch, "runtime.start", "runtime.stop", "runtime.execute")
    assert {"runtime.interrupt", "runtime.cancel"} <= set(
        scratch["operations"]
    )
    async with host.watch(stream(scratch)) as watch:
        await watch.ready()
        await start(host, scratch, watch)
        key = uuid4().hex
        run = await execute(
            host, scratch, "import time\ntime.sleep(60)", idempotency_key=key
        )
        await watch.until(
            lambda f: (
                f.event == "execution"
                and f.message()["execution"]["run_id"] == run["id"]
                and f.message()["execution"]["status"] == "running"
            )
        )
        queued = await execute(host, scratch, "pass")
        assert queued["id"] != run["id"]
        assert queued["status"] == "queued"
        control = {"notebook_id": scratch["id"]}
        if tool == "runtime.cancel":
            control["run_id"] = run["id"]
        control_key = uuid4().hex
        receipt = await host.call(tool, idempotency_key=control_key, **control)
        assert receipt["id"] == (
            run["runtime_id"] if tool == "runtime.interrupt" else run["id"]
        )
        completed = {}

        def both_completed(frame):
            if frame.event == "run":
                record = frame.message()["run"]
                if record["status"] == "completed":
                    completed[record["id"]] = record
            return run["id"] in completed and queued["id"] in completed

        # Either run may publish its terminal event first.
        events = await watch.until(both_completed)
        assert completed[run["id"]]["outcome"] == "interrupted"
        assert completed[queued["id"]]["outcome"] == (
            "interrupted" if tool == "runtime.interrupt" else "succeeded"
        )
        executions = objects(events, "execution")
        active_executions = {
            e["id"]: e for e in executions if e["run_id"] == run["id"]
        }
        assert active_executions
        assert all(
            e["status"] == "completed"
            and e["outcome"]["kind"] == "interrupted"
            for e in active_executions.values()
        )
        if tool == "runtime.interrupt":
            # The queued run may leave an execution, but it never started.
            assert not any(
                e["run_id"] == queued["id"] and e["started_at"] is not None
                for e in executions
            )
            replay = await host.call(
                tool, idempotency_key=control_key, **control
            )
            assert replay == receipt
        else:
            failed(await host.call(tool, **control), "conflict")


async def test_cancel_queued_run_leaves_other_runs_untouched(host, scratch):
    require(scratch, "runtime.start", "runtime.stop", "runtime.execute")
    assert "runtime.cancel" in scratch["operations"]

    async def cancel(run):
        return await host.call(
            "runtime.cancel", notebook_id=scratch["id"], run_id=run["id"]
        )

    async with host.watch(stream(scratch)) as watch:
        await watch.ready()
        await start(host, scratch, watch)
        active_key = uuid4().hex
        active = await execute(
            host,
            scratch,
            "import time\ntime.sleep(60)",
            idempotency_key=active_key,
        )
        await watch.until(
            lambda f: (
                f.event == "execution"
                and f.message()["execution"]["run_id"] == active["id"]
                and f.message()["execution"]["status"] == "running"
            )
        )
        queued = await execute(host, scratch, "pass")
        next_key = uuid4().hex
        successor = await execute(
            host, scratch, "pass", idempotency_key=next_key
        )
        assert queued["status"] == successor["status"] == "queued"
        assert (await cancel(queued))["id"] == queued["id"]
        events = await watch.until(
            lambda f: (
                f.event == "run"
                and f.message()["run"]["id"] == queued["id"]
                and f.message()["run"]["status"] == "completed"
            )
        )
        assert events[-1].message()["run"]["outcome"] == "interrupted"
        # The queued run may leave an execution, but it never started.
        assert not any(
            e["run_id"] == queued["id"] and e["started_at"] is not None
            for e in objects(events, "execution")
        )
        assert not any(
            r["id"] in (active["id"], successor["id"])
            and r["status"] == "completed"
            for r in objects(events, "run")
        )
        failed(await cancel(queued), "conflict")
        assert (await cancel(active))["id"] == active["id"]
        await finish_run(watch, active, active_key, "interrupted")
        await finish_run(watch, successor, next_key, "succeeded")


async def test_idle_interrupt_does_not_arm_future_work(host, scratch):
    require(scratch, "runtime.start", "runtime.stop", "runtime.execute")
    assert "runtime.interrupt" in scratch["operations"]
    async with host.watch(stream(scratch)) as watch:
        await watch.ready()
        runtime = await start(host, scratch, watch)
        receipt = await host.call(
            "runtime.interrupt", notebook_id=scratch["id"]
        )
        assert receipt["id"] == runtime["id"]
        assert receipt["status"] == "running"
        key = uuid4().hex
        run = await execute(host, scratch, "pass", idempotency_key=key)
        await finish_run(watch, run, key, "succeeded")
