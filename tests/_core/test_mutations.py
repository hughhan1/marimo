# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
from typing import Any

import msgspec
import pytest

from marimo._core import protocol
from marimo._core.calls import Caller, decode, dispatch
from marimo._core.operations import Mutations, Reply
from tests._core.test_notebook_document import put

NOTEBOOK = protocol.NotebookId("n1")


@pytest.fixture
def serve(bridge):
    mutations = Mutations(bridge.host)

    async def serve(name: str, key: str, **arguments: Any) -> Reply:
        call = decode(
            name,
            msgspec.to_builtins(
                {"idempotency_key": key, "notebook_id": NOTEBOOK, **arguments}
            ),
        )
        return await dispatch(mutations, bridge, name, call, Caller())

    return serve


def kind(reply: Reply) -> str:
    assert reply is not None
    kind: str = msgspec.to_builtins(reply)["type"]
    return kind.rsplit(":", 1)[1]


async def test_edit_retry_and_atomic_conflict(bridge, serve):
    events = []
    bridge.host.subscribe(events.append, notebook_id=NOTEBOOK)
    changes = [put("c1", "x = 2", "*")]
    result = await serve("notebook.edit", "edit", changes=changes)
    assert isinstance(result, protocol.EditResponse)
    event_count = len(events)
    assert event_count > 0
    assert await serve("notebook.edit", "edit", changes=changes) == result
    assert (bridge.edits, len(events)) == (1, event_count)

    conflict = await serve(
        "notebook.edit",
        "conflict",
        changes=[put("new", "y = 1", None), put("c1", "x = 3", "stale")],
    )
    assert isinstance(conflict, protocol.PreconditionFailedProblem)
    assert conflict.status == 412
    assert conflict.index == 1
    assert conflict.cell.code == "x = 2"
    assert len(events) == event_count
    notebook = await bridge.host.notebook(NOTEBOOK)
    assert [cell.id for cell in notebook.document] == ["c1"]


async def test_cancellation_and_concurrent_retry(bridge, serve):
    bridge.release.clear()
    changes = [put("c1", "x = 2", "*")]
    first = asyncio.create_task(
        serve("notebook.edit", "edit", changes=changes)
    )
    await asyncio.wait_for(bridge.started.wait(), 5)
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        retry = asyncio.create_task(
            serve("notebook.edit", "edit", changes=changes)
        )
        bridge.release.set()
        result = await asyncio.wait_for(retry, 5)
        assert isinstance(result, protocol.EditResponse)
        assert bridge.edits == 1
    finally:
        bridge.release.set()


async def test_receipts_include_tool_and_preconditions(bridge, serve):
    saved = await serve("notebook.save", "save", expected_etag="a")
    assert isinstance(saved, protocol.EditResponse)
    assert await serve("notebook.save", "save", expected_etag="a") == saved
    assert kind(await serve("notebook.save", "save", expected_etag="c")) == (
        "conflict"
    )
    assert kind(await serve("notebook.edit", "save", changes=[])) == "conflict"
    assert bridge.preconditions == [("a",)]


async def test_rejected_calls_do_not_reserve_keys(serve):
    stale = await serve(
        "notebook.edit", "edit", changes=[put("c1", "x = 2", "stale")]
    )
    assert kind(stale) == "precondition-failed"
    assert isinstance(
        await serve(
            "notebook.edit", "edit", changes=[put("c1", "x = 2", "*")]
        ),
        protocol.EditResponse,
    )


async def test_calls_check_tools_capabilities_and_keys(bridge, serve):
    assert kind(await serve("runtime.stop", "stop")) == "unsupported-operation"
    assert kind(await serve("notebook.edit", "", changes=[])) == (
        "invalid-request"
    )
    # Only a host serves the catalog's tools.
    assert kind(await serve("notebook.delete", "delete")) == "invalid-request"
    bridge.host.update_notebook(
        msgspec.structs.replace(bridge.host.notebooks[NOTEBOOK], operations=())
    )
    assert kind(await serve("notebook.edit", "edit", changes=[])) == (
        "forbidden"
    )
    assert bridge.edits == 0
