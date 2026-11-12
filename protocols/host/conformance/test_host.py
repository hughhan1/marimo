# Copyright 2026 Marimo. All rights reserved.
"""Check a running host through its tools and events."""

from __future__ import annotations

import asyncio
from itertools import groupby
from typing import Any
from uuid import uuid4

import pytest
from contract import (
    Failure,
    Refused,
    document_state,
    failed,
    objects,
    problem,
)


def follow(host, notebook, **arguments: Any):
    return host.subscribe("notebook", notebook_id=notebook["id"], **arguments)


async def snapshot(host, notebook, **arguments: Any):
    return await host.snapshot(
        "notebook", notebook_id=notebook["id"], **arguments
    )


def require(notebook, *operations: str):
    missing = set(operations) - set(notebook["operations"])
    if missing:
        pytest.skip(f"notebook does not offer {', '.join(sorted(missing))}")


def live(event, key):
    assert event.id, "live event has no cursor"
    assert event.data.get("time"), "live event has no timestamp"
    assert event.data.get("request_id") == key, (
        "event has the wrong request_id"
    )
    return event.data


async def test_required_operations(catalog, pytestconfig):
    available = set(objects(catalog, "host")[0]["operations"])
    assert set(pytestconfig.getoption("--require-operation")) <= available


# The host authenticates before MCP, so it refuses with an HTTP problem.
SUBSCRIBE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "events/stream",
    "params": {"name": "catalog", "arguments": {}},
}


@pytest.mark.parametrize("authorization", ["", "Bearer wrong-token"])
async def test_security_token(host, authorization):
    response = await host.client.post(
        "mcp", headers={"Authorization": authorization}, json=SUBSCRIBE
    )
    problem(response, 401, "unauthorized")


async def test_security_cookie(host, record):
    response = await host.client.post(
        "mcp",
        headers={"Authorization": "", "Cookie": f"token={record['token']}"},
        json=SUBSCRIBE,
    )
    problem(response, 401, "unauthorized")


async def test_security_cors(host):
    response = await host.client.options(
        "mcp",
        headers={
            "Origin": "https://example.invalid",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert "access-control-allow-origin" not in response.headers


async def test_catalog_snapshot(catalog):
    assert catalog[0].event == "host"
    assert catalog[-1].event == "ready"
    assert catalog[-1].id
    assert len(objects(catalog, "host")) == 1
    projects, notebooks = set(), set()
    for event in catalog[:-1]:
        assert event.id is None, "snapshot event has an id"
        if event.event == "project":
            projects.add(event.data["project"]["id"])
        elif event.event == "notebook":
            notebook = event.data["notebook"]
            assert notebook["project_id"] in projects
            notebooks.add(notebook["id"])
        elif event.event in ("runtime", "attachment"):
            assert event.data[event.event]["notebook_id"] in notebooks


async def test_types_filter(host):
    events = await host.snapshot("catalog", types=["runtime"])
    assert {event.event for event in events} <= {"runtime", "ready"}


async def test_resume(host, catalog):
    resumed = await host.snapshot("catalog", cursor=catalog[-1].id)
    assert resumed[-1].id
    if resumed[0].event == "reset":
        assert all(event.id is None for event in resumed[:-1])
        assert resumed[1].event == "host"
    else:
        assert all(event.id for event in resumed), (
            "resume sent a snapshot without reset"
        )


async def test_unknown_cursor_resets(host):
    # Such as a cursor from before the host lost its state.
    events = await host.snapshot("catalog", cursor="conformance-unknown")
    assert [event.event for event in events[:2]] == ["reset", "host"]
    assert all(event.id is None for event in events[:-1])
    assert events[-1].event == "ready"
    assert events[-1].id


@pytest.mark.parametrize(
    ("name", "arguments", "kind"),
    [
        ("catalog", {"types": ["conformance"]}, "invalid-request"),
        ("catalog", {"cursor": ""}, "invalid-request"),
        ("notebook", {"notebook_id": "conformance-missing"}, "not-found"),
    ],
)
async def test_refusals(host, name, arguments, kind):
    failed(await host.snapshot(name, **arguments), kind)


@pytest.mark.parametrize(
    "arguments",
    [
        {"run_id": ""},
        {"attachment_identity": ""},
        {"attachment_kind": "Not a kind"},
        {"attachment_name": "x" * 257},
    ],
    ids=lambda arguments: next(iter(arguments)),
)
async def test_notebook_refusals(host, scratch, arguments):
    failed(await snapshot(host, scratch, **arguments), "invalid-request")


async def test_unknown_arguments_are_invalid_params(host):
    with pytest.raises(Refused) as refused:
        async with host.subscribe("catalog", conformance=True):
            pytest.fail("the host accepted an unknown argument")
    assert refused.value.error["code"] == -32602


async def test_notebook_document(host, catalog):
    notebooks = objects(catalog, "notebook")
    if not notebooks:
        pytest.skip("host has no notebooks")
    events = await snapshot(host, notebooks[0])
    document_state(events)
    assert objects(events, "attachment"), "notebook stream has no attachment"
    filtered = await snapshot(host, notebooks[0], types=["cell", "document"])
    assert {event.event for event in filtered} <= {"cell", "document", "ready"}
    document_state(filtered)


async def test_each_subscription_attaches_until_it_ends(host, scratch):
    named = {"identity": "conformance", "kind": "agent", "name": "Conformance"}
    arguments = {f"attachment_{key}": value for key, value in named.items()}

    async def attached(observer):
        events = await observer.until(lambda e: e.event == "attachment")
        return live(events[-1], None)["attachment"]

    async def detached(observer, attachment):
        await observer.until(
            lambda e: (
                e.event == "attachment.removed"
                and live(e, None)["id"] == attachment["id"]
            )
        )

    async with follow(host, scratch, types=["attachment"]) as observer:
        # The scratch notebook's only attachment is the observer's.
        [own] = objects(await observer.ready(), "attachment")
        assert own["notebook_id"] == scratch["id"]
        assert (own["identity"], own["kind"], own["name"]) == (
            None,
            "client",
            None,
        )
        async with follow(host, scratch, **arguments) as first:
            await first.ready()
            one = await attached(observer)
            # The same arguments still make a separate attachment.
            async with follow(host, scratch, **arguments) as second:
                await second.ready()
                two = await attached(observer)
            await detached(observer, two)
        await detached(observer, one)
    assert one["id"] != two["id"]
    for attachment in (one, two):
        assert {key: attachment[key] for key in named} == named


async def test_resume_replays_missed_events(host, scratch):
    require(scratch, "notebook.edit")
    kinds = ["cell", "document"]
    before = await snapshot(host, scratch, types=kinds)
    key = uuid4().hex
    answer = await edit(
        host, scratch, [put("check_a", "x = 1")], idempotency_key=key
    )
    replay = await snapshot(host, scratch, types=kinds, cursor=before[-1].id)
    assert [event.event for event in replay] == [*kinds, "ready"]
    cell, document = (live(event, key) for event in replay[:2])
    assert cell["cell"]["etag"] == answer["cells"]["check_a"]["etag"]
    assert document["document"] == answer["document"]
    assert replay[-1].id


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
    async with follow(host, scratch) as stream:
        await stream.ready()
        answer = await edit(
            host, scratch, [put("check_a", "x = 1")], idempotency_key=key
        )
        events = await stream.until(lambda e: e.event == "document")
        assert [e.event for e in events] == ["cell", "document"]
        for event in events:
            live(event, key)
        assert events[-1].data["document"] == answer["document"]
        assert (
            answer["cells"]["check_a"]["etag"]
            == events[0].data["cell"]["etag"]
        )
    before, cells = document_state(await snapshot(host, scratch))
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
    after, after_cells = document_state(await snapshot(host, scratch))
    assert (after, after_cells) == (before, cells)
    failed(
        await edit(
            host,
            scratch,
            [{"kind": "set_order", "base_etag": "*", "order": []}],
        ),
        "invalid-request",
    )
    assert document_state(await snapshot(host, scratch)) == (
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
    _, cells = document_state(await snapshot(host, scratch))
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


async def start(host, notebook, stream):
    key = uuid4().hex
    runtime = await host.call(
        "runtime.start", notebook_id=notebook["id"], idempotency_key=key
    )
    if isinstance(runtime, Failure):
        failed(runtime, "decision-required")
        pytest.skip("runtime requires a user decision")
    assert runtime["status"] == "starting"
    events = await stream.until(
        lambda e: (
            e.event == "runtime"
            and e.data["runtime"]["id"] == runtime["id"]
            and e.data["runtime"]["status"] in ("running", "failed")
        )
    )
    assert events[-1].data["runtime"]["status"] == "running"
    live(events[-1], key)
    return runtime


async def finish_run(stream, run, key, outcome):
    events = await stream.until(
        lambda e: (
            e.event == "run"
            and e.data["run"]["id"] == run["id"]
            and e.data["run"]["status"] == "completed"
        )
    )
    executions = {}
    for event in events:
        if event.event == "execution":
            execution = event.data["execution"]
            if execution["run_id"] == run["id"]:
                live(event, key)
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
    async with follow(host, scratch) as stream:
        await stream.ready()
        await start(host, scratch, stream)
        key = uuid4().hex
        run = await execute(host, scratch, code, idempotency_key=key)
        assert run["status"] in ("queued", "running")
        assert run["id"] != key
        events, executions = await finish_run(stream, run, key, outcome)
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
    # `run_id` narrows a subscription to the run and its records.
    narrowed = await snapshot(host, scratch, run_id=run["id"])
    assert [r["id"] for r in objects(narrowed, "run")] == [run["id"]]
    assert {e["id"] for e in objects(narrowed, "execution")} == {
        e["id"] for e in executions
    }
    assert not {e.event for e in narrowed} & {
        "notebook",
        "cell",
        "document",
        "runtime",
        "attachment",
    }


async def test_runtime_lifecycle(host, scratch):
    require(scratch, "runtime.start", "runtime.stop", "runtime.restart")
    async with follow(host, scratch) as stream:
        await stream.ready()
        runtime = await start(host, scratch, stream)
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
        events = await stream.until(
            lambda e: (
                e.event == "runtime"
                and e.data["runtime"]["id"] == replacement["id"]
                and e.data["runtime"]["status"] in ("running", "failed")
            )
        )
        restart_events = [
            event
            for event in events
            if event.event in ("runtime", "runtime.removed")
            and event.data.get("request_id") == key
        ]
        transitions = [
            (e.event, e.data.get("runtime", {}).get("status"))
            for e in restart_events
        ]
        assert [transition for transition, _ in groupby(transitions)] == [
            ("runtime", "terminating"),
            ("runtime.removed", None),
            ("runtime", "starting"),
            ("runtime", "running"),
        ]
        for event in restart_events:
            live(event, key)
        key = uuid4().hex
        stopped = await host.call(
            "runtime.stop", notebook_id=scratch["id"], idempotency_key=key
        )
        assert stopped["id"] == replacement["id"]
        events = await stream.until(lambda e: e.event == "runtime.removed")
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
    async with follow(host, scratch) as stream:
        await stream.ready()
        await start(host, scratch, stream)
        key = uuid4().hex
        run = await host.call(
            "runtime.run",
            notebook_id=scratch["id"],
            idempotency_key=key,
            cells=[
                {"id": "check_a", "etag": answer["cells"]["check_a"]["etag"]}
            ],
        )
        _, executions = await finish_run(stream, run, key, "succeeded")
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
    async with follow(host, scratch) as stream:
        await stream.ready()
        await start(host, scratch, stream)
        key = uuid4().hex
        run = await execute(
            host, scratch, "import time\ntime.sleep(60)", idempotency_key=key
        )
        await stream.until(
            lambda e: (
                e.event == "execution"
                and e.data["execution"]["run_id"] == run["id"]
                and e.data["execution"]["status"] == "running"
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

        def both_completed(event):
            if event.event == "run":
                record = event.data["run"]
                if record["status"] == "completed":
                    completed[record["id"]] = record
            return run["id"] in completed and queued["id"] in completed

        # Either run may publish its terminal event first.
        events = await stream.until(both_completed)
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

    async with follow(host, scratch) as stream:
        await stream.ready()
        await start(host, scratch, stream)
        active_key = uuid4().hex
        active = await execute(
            host,
            scratch,
            "import time\ntime.sleep(60)",
            idempotency_key=active_key,
        )
        await stream.until(
            lambda e: (
                e.event == "execution"
                and e.data["execution"]["run_id"] == active["id"]
                and e.data["execution"]["status"] == "running"
            )
        )
        queued = await execute(host, scratch, "pass")
        next_key = uuid4().hex
        successor = await execute(
            host, scratch, "pass", idempotency_key=next_key
        )
        assert queued["status"] == successor["status"] == "queued"
        assert (await cancel(queued))["id"] == queued["id"]
        events = await stream.until(
            lambda e: (
                e.event == "run"
                and e.data["run"]["id"] == queued["id"]
                and e.data["run"]["status"] == "completed"
            )
        )
        assert events[-1].data["run"]["outcome"] == "interrupted"
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
        await finish_run(stream, active, active_key, "interrupted")
        await finish_run(stream, successor, next_key, "succeeded")


async def test_idle_interrupt_does_not_arm_future_work(host, scratch):
    require(scratch, "runtime.start", "runtime.stop", "runtime.execute")
    assert "runtime.interrupt" in scratch["operations"]
    async with follow(host, scratch) as stream:
        await stream.ready()
        runtime = await start(host, scratch, stream)
        receipt = await host.call(
            "runtime.interrupt", notebook_id=scratch["id"]
        )
        assert receipt["id"] == runtime["id"]
        assert receipt["status"] == "running"
        key = uuid4().hex
        run = await execute(host, scratch, "pass", idempotency_key=key)
        await finish_run(stream, run, key, "succeeded")
