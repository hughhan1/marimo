# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import msgspec
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from marimo._ast.app_config import _AppConfig
from marimo._ast.cell import CellConfig
from marimo._config.config import DEFAULT_CONFIG
from marimo._core.calls import Caller, decode, dispatch
from marimo._messaging.notebook import CreateCell, Transaction
from marimo._messaging.notification import (
    ModelLifecycleNotification,
    ModelOpen,
    NotebookDocumentTransactionNotification,
    VariableDeclarationNotification,
    VariablesNotification,
)
from marimo._messaging.serde import serialize_kernel_message
from marimo._pyodide.bootstrap import instantiate
from marimo._pyodide.pyodide_session import PyodideBridge, PyodideSession
from marimo._runtime.commands import (
    AppMetadata,
    CreateNotebookCommand,
    EditCellsCommand,
    ModelCommand,
    ModelUpdateMessage,
)
from marimo._runtime.layout.layout import LayoutConfig
from marimo._session.model import SessionMode
from marimo._session.notebook import AppFileManager
from marimo._types.ids import CellId_t, WidgetModelId


@pytest.fixture
def bridge(tmp_path: Path):
    filename = tmp_path / "notebook.py"
    filename.write_text("""import marimo
app = marimo.App()
@app.cell
def _():
    x = 1
    return (x,)
""")
    session = PyodideSession(
        AppFileManager(str(filename)),
        SessionMode.EDIT,
        AppMetadata(
            query_params={},
            cli_args={},
            app_config=_AppConfig(),
            filename=str(filename),
        ),
        DEFAULT_CONFIG,
    )
    bridge = PyodideBridge(session)
    yield bridge
    bridge.close()


async def snapshot(
    bridge, subscription="reader", cursor=None, notifications=None
):
    """Follow host events, and marimo notifications into `notifications`."""
    events = []

    def receive(raw):
        event = json.loads(raw)
        if event["event"] != "marimo":
            events.append(event)
        elif notifications is not None:
            notifications.append(event["data"]["marimo"])

    await bridge.notebook.subscribe(subscription, cursor, receive)
    return events


def ops(notifications):
    return [notification["op"] for notification in notifications]


async def send(bridge, call):
    """Call a notebook tool as the page does, for its `{ok, data|problem}`."""
    return json.loads(await bridge.notebook.call(json.dumps(call)))


def edit(events, code="x = 2", request_id="edit-1"):
    cell = next(
        event["data"]["cell"] for event in events if event["event"] == "cell"
    )
    return {
        "operation": "notebook.edit",
        "idempotency_key": request_id,
        "notebook_id": cell["notebook_id"],
        "changes": [
            {
                "kind": "put_cell",
                "base_etag": cell["etag"],
                "cell": {
                    "id": cell["id"],
                    "code": code,
                    "name": cell["name"],
                    "config": cell["config"],
                },
            }
        ],
    }


async def test_snapshot_edit_and_retry(bridge):
    events = await snapshot(bridge)
    assert [event["event"] for event in events] == [
        "notebook",
        "cell",
        "document",
        "runtime",
        "ready",
    ]
    assert events[0]["data"]["notebook"]["operations"] == [
        "notebook.edit",
        "notebook.save",
        "runtime.run",
        "runtime.input",
        "runtime.execute",
        "runtime.interrupt",
        "runtime.cancel",
    ]
    request = edit(events)
    response = await send(bridge, request)
    assert response["ok"], response
    assert response["data"]["document"] == events[-1]["data"]["document"]
    assert [event["event"] for event in events[-2:]] == ["cell", "document"]
    assert all(
        event["data"]["request_id"] == "edit-1" for event in events[-2:]
    )
    assert list(bridge.session.app_manager.app.cell_manager.codes()) == [
        "x = 2"
    ]
    count = len(events)
    assert await send(bridge, request) == response
    assert len(events) == count
    # A call dispatched directly shares the worker's retry receipts.
    arguments = {k: v for k, v in request.items() if k != "operation"}
    direct = await dispatch(
        bridge.notebook.mutations,
        bridge.notebook,
        "notebook.edit",
        decode("notebook.edit", arguments),
        Caller(),
    )
    assert msgspec.json.decode(msgspec.json.encode(direct)) == response["data"]
    assert len(events) == count
    conflict = await send(bridge, edit(events, "x = 3"))
    assert conflict["problem"]["type"].endswith(":conflict")
    assert list(bridge.session.app_manager.app.cell_manager.codes()) == [
        "x = 2"
    ]


async def test_atomic_conflict_and_kernel_update(bridge):
    events = await snapshot(bridge)
    instantiate(bridge.session, auto_instantiate=False)
    queue = bridge.session._queue_manager.control_queue
    assert isinstance(queue.get_nowait(), CreateNotebookCommand)
    await send(bridge, edit(events))
    update = queue.get_nowait()
    assert isinstance(update, EditCellsCommand)
    cell_id = events[1]["data"]["cell"]["id"]
    assert update.codes == {cell_id: "x = 2"}
    request = edit(events, "x = 3", request_id="conflict")
    request["changes"].insert(
        0,
        {
            "kind": "put_cell",
            "base_etag": None,
            "after": cell_id,
            "cell": {"id": "new", "code": "y = 1"},
        },
    )
    problem = (await send(bridge, request))["problem"]
    assert problem["type"].endswith(":precondition-failed")
    assert problem["index"] == 1
    assert problem["cell"]["code"] == "x = 2"
    assert list(bridge.session.app_manager.app.cell_manager.codes()) == [
        "x = 2"
    ]
    assert queue.empty()


async def test_edit_before_instantiation(bridge):
    events = await snapshot(bridge)
    await send(bridge, edit(events))
    queue = bridge.session._queue_manager.control_queue
    assert queue.empty()
    instantiate(bridge.session, auto_instantiate=False)
    command = queue.get_nowait()
    assert isinstance(command, CreateNotebookCommand)
    assert [request.code for request in command.execution_requests] == [
        "x = 2"
    ]
    assert queue.empty()


async def test_resume_and_independent_unsubscribe(bridge):
    first = await snapshot(bridge, "first")
    cursor = first[-1]["id"]
    await send(bridge, edit(first))
    second = await snapshot(bridge, "second", cursor)
    assert [event["event"] for event in second] == [
        "cell",
        "document",
        "ready",
    ]
    bridge.notebook.unsubscribe("first")
    count = len(first)
    await send(bridge, edit(second, "x = 3", request_id="edit-2"))
    assert len(first) == count
    assert second[-2]["data"]["cell"]["code"] == "x = 3"
    reset = await snapshot(bridge, "third", "another-host")
    assert [event["event"] for event in reset] == [
        "reset",
        "notebook",
        "cell",
        "document",
        "runtime",
        "ready",
    ]
    bridge.close()
    with pytest.raises(RuntimeError, match="closed"):
        await snapshot(bridge)
    assert bridge.notebook._subscriptions == {}


async def test_permissions_and_unsupported_operations(bridge):
    bridge.session.mode = SessionMode.RUN
    events = await snapshot(bridge)
    assert events[0]["data"]["notebook"]["operations"] == []
    request = edit(events)
    save = {
        "operation": "notebook.save",
        "idempotency_key": "save",
        "notebook_id": request["notebook_id"],
    }
    for call in (request, save):
        reply = await send(bridge, call)
        assert reply["problem"]["type"].endswith(":forbidden")


async def test_invalid_calls(bridge):
    request = edit(await snapshot(bridge))
    unnamed = {
        key: value for key, value in request.items() if key != "notebook_id"
    }
    for invalid in (
        {**request, "body": "not an argument"},
        {**request, "operation": "notebook.missing"},
        # Only a host serves the catalog's tools.
        {**unnamed, "operation": "notebook.create", "project_id": "p"},
        unnamed,
    ):
        reply = await send(bridge, invalid)
        assert reply["problem"]["status"] == 400
        assert reply["problem"]["type"].endswith(":invalid-request")
    malformed = json.loads(await bridge.notebook.call("{"))
    assert malformed["problem"]["status"] == 400
    other = {**request, "notebook_id": "elsewhere"}
    assert (await send(bridge, other))["problem"]["type"].endswith(
        ":not-found"
    )


async def test_kernel_ready_follows_the_snapshot(bridge):
    frames = []
    await bridge.notebook.subscribe(
        "reader", None, lambda raw: frames.append(json.loads(raw))
    )
    kinds = [frame["event"] for frame in frames]
    assert kinds[:5] == ["notebook", "cell", "document", "runtime", "ready"]
    assert set(kinds[5:]) == {"marimo"}
    kernel_ready = frames[5]["data"]["marimo"]
    assert kernel_ready["op"] == "kernel-ready"
    assert "codes" not in kernel_ready
    assert kernel_ready["resumed"] is False


async def test_a_later_subscription_resumes_with_retained_notifications(
    bridge,
):
    session = bridge.session
    session.instantiated = True
    variables = VariablesNotification(
        variables=[
            VariableDeclarationNotification(
                name="x", declared_by=[], used_by=[]
            )
        ]
    )
    session._on_message(serialize_kernel_message(variables))
    notifications = []
    await snapshot(bridge, notifications=notifications)
    assert ops(notifications)[0] == "kernel-ready"
    assert notifications[0]["resumed"] is True
    assert "variables" in ops(notifications)


async def test_resubscription_preserves_frontend_widget_updates(bridge):
    model_id = WidgetModelId("widget-1")
    await snapshot(bridge)
    bridge.session._on_message(
        serialize_kernel_message(
            ModelLifecycleNotification(
                model_id=model_id,
                message=ModelOpen(
                    state={"value": 1}, buffer_paths=[], buffers=[]
                ),
            )
        )
    )
    bridge.session.put_control_request(
        ModelCommand(
            model_id=model_id,
            message=ModelUpdateMessage(state={"value": 7}, buffer_paths=[]),
            buffers=[],
        )
    )
    bridge.notebook.unsubscribe("reader")
    notifications = []
    await snapshot(bridge, notifications=notifications)

    assert [
        notification
        for notification in notifications
        if notification["op"] == "model-lifecycle"
    ] == [
        {
            "op": "model-lifecycle",
            "model_id": model_id,
            "message": {
                "method": "open",
                "state": {"value": 7},
                "buffer_paths": [],
                "buffers": [],
                "esm_spec": None,
            },
        }
    ]


async def test_live_notifications_reach_each_subscription_until_it_closes(
    bridge,
):
    first, second = [], []
    await snapshot(bridge, "first", notifications=first)
    await snapshot(bridge, "second", notifications=second)
    variables = VariablesNotification(variables=[])
    bridge.session._on_message(serialize_kernel_message(variables))
    assert ops(first)[-1] == ops(second)[-1] == "variables"
    bridge.notebook.unsubscribe("first")
    count = len(first)
    bridge.session._on_message(serialize_kernel_message(variables))
    assert len(first) == count
    assert ops(second)[-2:] == ["variables", "variables"]
    bridge.notebook.unsubscribe("second")
    # The runtime backend stays attached; only the subscriptions leave.
    assert bridge.session.consumers[:2] == [
        bridge.session.session_view.add_raw_notification,
        bridge._on_message,
    ]
    backend = bridge.notebook._backend
    assert backend is not None
    assert bridge.session.consumers[2:] == [backend._notify]


def save_call(bridge, key="save-1", expected_etag=None):
    call = {
        "operation": "notebook.save",
        "idempotency_key": key,
        "notebook_id": bridge.notebook.id,
    }
    if expected_etag is not None:
        call["expected_etag"] = expected_etag
    return call


async def test_save_keeps_newer_edits_and_kernel_updates(bridge):
    events = await snapshot(bridge)
    instantiate(bridge.session, auto_instantiate=False)
    queue = bridge.session._queue_manager.control_queue
    queue.get_nowait()
    await send(bridge, edit(events))
    queue.get_nowait()
    notebook = await bridge.notebook.host.notebook(bridge.notebook.id)
    saved_revision = notebook.document.etag
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def persist(contents, _filename):
        writes.append(contents)
        started.set()
        await release.wait()

    bridge.notebook.set_persistence(persist)
    request = save_call(bridge)
    saving = asyncio.create_task(send(bridge, request))
    await started.wait()
    # A second subscriber observes the updated cell's current etag.
    await send(
        bridge, edit(await snapshot(bridge, "editor"), "x = 3", "edit-2")
    )
    update = queue.get_nowait()
    assert isinstance(update, EditCellsCommand)
    assert list(update.codes.values()) == ["x = 3"]
    release.set()
    reply = await saving
    assert reply["ok"], reply
    assert reply["data"]["document"]["saved_etag"] == saved_revision
    assert notebook.document.etag != notebook.saved_etag
    assert list(bridge.session.app_manager.app.cell_manager.codes()) == [
        "x = 3"
    ]
    assert len(writes) == 1
    assert "x = 2" in writes[0]
    assert "x = 3" not in writes[0]
    assert queue.empty()
    # A lost reply retries the same save, rather than saving the newer document.
    assert await send(bridge, request) == reply
    assert len(writes) == 1


async def test_failed_save_stays_dirty_and_can_retry(bridge):
    events = await snapshot(bridge)
    await send(bridge, edit(events))
    notebook = await bridge.notebook.host.notebook(bridge.notebook.id)
    saved = notebook.saved_etag
    persist = AsyncMock(side_effect=OSError("storage full"))
    bridge.notebook.set_persistence(persist)
    request = save_call(bridge)
    count = len(events)
    failed = (await send(bridge, request))["problem"]
    assert failed["type"].endswith(":conflict")
    assert "storage full" in failed["detail"]
    assert notebook.saved_etag == saved != notebook.document.etag
    assert len(events) == count
    persist.side_effect = None
    assert (await send(bridge, request))["ok"]
    assert notebook.saved_etag == notebook.document.etag
    assert persist.await_count == 2


async def test_save_serializes_writes_and_checks_revision_after_waiting(
    bridge,
):
    events = await snapshot(bridge)
    notebook = await bridge.notebook.host.notebook(bridge.notebook.id)
    original = notebook.document.etag
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def persist(contents, _filename):
        writes.append(contents)
        started.set()
        await release.wait()

    bridge.notebook.set_persistence(persist)
    first = asyncio.create_task(send(bridge, save_call(bridge)))
    await started.wait()
    stale = asyncio.create_task(
        send(bridge, save_call(bridge, "stale", original))
    )
    await send(bridge, edit(events))
    second = asyncio.create_task(send(bridge, save_call(bridge, "save-2")))
    await asyncio.sleep(0)
    assert len(writes) == 1
    release.set()
    assert (await first)["ok"]
    stale_problem = (await stale)["problem"]
    assert stale_problem["type"].endswith(":precondition-failed")
    assert (await second)["ok"]
    assert len(writes) == 2
    assert "x = 2" in writes[1]
    assert notebook.saved_etag == notebook.document.etag


async def test_host_edits_config_layout_and_reload(bridge):
    events = await snapshot(bridge)
    cell = next(e["data"]["cell"] for e in events if e["event"] == "cell")
    edited = await send(
        bridge,
        {
            "operation": "notebook.edit",
            "idempotency_key": "edit-1",
            "notebook_id": bridge.notebook.id,
            "changes": [
                {
                    "kind": "put_cell",
                    "base_etag": cell["etag"],
                    "cell": {
                        "id": cell["id"],
                        "code": "x = 9",
                        "name": "example",
                        "config": cell["config"],
                    },
                },
                {
                    "kind": "put_cell",
                    "base_etag": None,
                    "before": cell["id"],
                    "cell": {"id": "new", "code": "y = 2"},
                },
            ],
        },
    )
    assert edited["ok"], edited
    await bridge.save_app_config(json.dumps({"config": {"width": "full"}}))
    layout = {"type": "slides", "data": {"slides": [[cell["id"]]]}}
    await bridge.notebook.set_layout(json.dumps(layout))
    path = bridge.session.app_manager.filename

    async def persist(contents, filename):
        from pathlib import Path

        await asyncio.to_thread(Path(filename).write_text, contents)

    bridge.notebook.set_persistence(persist)
    assert (await send(bridge, save_call(bridge)))["ok"]
    restored = AppFileManager(path)
    assert list(restored.app.cell_manager.codes()) == ["y = 2", "x = 9"]
    assert list(restored.app.cell_manager.names()) == ["_", "example"]
    assert restored.app.config.width == "full"
    assert restored.read_layout_config() == LayoutConfig(**layout)
    deleted = await send(
        bridge,
        {
            "operation": "notebook.edit",
            "idempotency_key": "edit-2",
            "notebook_id": bridge.notebook.id,
            "changes": [
                {
                    "kind": "delete_cell",
                    "id": "new",
                    "base_etag": edited["data"]["cells"]["new"]["etag"],
                }
            ],
        },
    )
    assert deleted["ok"], deleted
    assert list(bridge.session.app_manager.app.cell_manager.codes()) == [
        "x = 9"
    ]


async def test_app_config_changes_are_written_when_persistence_is_set(bridge):
    await snapshot(bridge)
    notebook = await bridge.notebook.host.notebook(bridge.notebook.id)
    persist = AsyncMock()
    bridge.notebook.set_persistence(persist)
    await bridge.save_app_config(json.dumps({"config": {"width": "full"}}))
    persist.assert_awaited_once()
    contents, _filename = persist.await_args.args
    assert 'width="full"' in contents
    assert notebook.saved_etag == notebook.document.etag


async def test_save_includes_kernel_document_edits(bridge):
    await snapshot(bridge)
    persist = AsyncMock()
    bridge.notebook.set_persistence(persist)
    bridge.session._on_message(
        serialize_kernel_message(
            NotebookDocumentTransactionNotification(
                transaction=Transaction(
                    changes=(
                        CreateCell(
                            cell_id=CellId_t("kernel-cell"),
                            code="y = 8",
                            name="_",
                            config=CellConfig(),
                        ),
                    ),
                    source="kernel",
                )
            )
        )
    )
    assert (await send(bridge, save_call(bridge)))["ok"]
    contents, _filename = persist.call_args.args
    assert "y = 8" in contents
    assert list(bridge.session.app_manager.app.cell_manager.codes()) == [
        "x = 1",
        "y = 8",
    ]
    assert bridge.session._queue_manager.control_queue.empty()
