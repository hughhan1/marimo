# Copyright 2026 Marimo. All rights reserved.
"""An untitled editor's session, followed through its `session` event."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from inline_snapshot import snapshot
from starlette.websockets import WebSocketDisconnect

from marimo._server.host.bridge import SessionBridge
from marimo._server.host.context import HostContext
from marimo._types.ids import CellId_t, SessionId
from tests._server.api.endpoints.ws_helpers import headers
from tests._server.host.drive import socket_snapshot
from tests._server.mcp_client import call_tool
from tests._server.mocks import get_session_manager, run_cells
from tests._server.session_events import connect_session

if TYPE_CHECKING:
    from starlette.testclient import TestClient

    from tests._server.session_events import SessionEvents


def first_cell_id(client: TestClient, session_id: str) -> CellId_t:
    """The first cell of the session's document, as the host stream has it."""
    session = get_session_manager(client).get_session(SessionId(session_id))
    assert session is not None
    return session.document.cell_ids[0]


def receive_notification(websocket: SessionEvents, op: str) -> dict[str, Any]:
    while True:
        event = websocket.receive_json()
        assert "op" not in event
        if event["event"] == "marimo" and event["data"]["marimo"]["op"] == op:
            return event["data"]["marimo"]


def assert_reconnect_alert(websocket: SessionEvents) -> None:
    assert websocket.receive_json() == snapshot(
        {
            "event": "marimo",
            "data": {
                "marimo": {
                    "op": "alert",
                    "title": "Reconnected",
                    "description": "You have reconnected to an existing session.",
                    "variant": None,
                }
            },
        }
    )


def completed(event: dict[str, Any], run_id: str) -> bool:
    """Whether `event` is the kernel's completed-run for the host run."""
    return (
        event["event"] == "marimo"
        and event["data"]["marimo"]["op"] == "completed-run"
        and event["data"]["marimo"]["run_id"] == run_id
    )


def receive_completed_run(websocket: SessionEvents, run_id: str) -> None:
    """Read until the host run's cycle has ended; an edit's cycle may come first."""
    while not completed(websocket.receive_json(), run_id):
        pass


def test_untitled_editor_runs_reconnects_and_restarts_its_runtime(
    client: TestClient,
) -> None:
    from marimo._server.workspace import EmptyWorkspace

    manager = get_session_manager(client)
    manager.workspace = EmptyWorkspace()
    bridge = SessionBridge(manager)
    client.app.state.host_context = HostContext(
        bridge=bridge, token="host-token", port=1234
    )
    url = "/ws?session_id=new&access_token=fake-token"
    with client:
        assert client.portal is not None
        client.portal.call(bridge.open)
        try:
            with connect_session(client, url) as ws:
                snapshot = socket_snapshot(ws)
                receive_notification(ws, "kernel-ready")
                notebook_id = next(
                    e["data"]["notebook"]["id"]
                    for e in snapshot
                    if e["event"] == "notebook"
                )
                cell_id = first_cell_id(client, "new")
                response = run_cells(
                    client,
                    notebook_id,
                    {cell_id: "x = 42"},
                    headers=headers("new"),
                )
                assert response.ok, response.problem
                receive_completed_run(ws, response.data["id"])
            with connect_session(client, url) as ws:
                snapshot = socket_snapshot(ws)
                assert_reconnect_alert(ws)
                assert (
                    next(
                        e["data"]["notebook"]["id"]
                        for e in snapshot
                        if e["event"] == "notebook"
                    )
                    == notebook_id
                )
                restarted = call_tool(
                    client,
                    "runtime.restart",
                    headers=headers("new"),
                    idempotency_key="restart",
                    notebook_id=notebook_id,
                )
                assert restarted.ok, restarted.problem
                replacement = restarted.data["id"]
                while True:
                    try:
                        ws.receive_json()
                    except WebSocketDisconnect as exc:
                        disconnect = exc.reason
                        break
                assert disconnect == "MARIMO_SHUTDOWN"
            with connect_session(client, url) as ws:
                snapshot = socket_snapshot(ws)
                assert (
                    next(
                        e["data"]["notebook"]["id"]
                        for e in snapshot
                        if e["event"] == "notebook"
                    )
                    == notebook_id
                )
                runtime = next(
                    e["data"]["runtime"]
                    for e in reversed(snapshot)
                    if e["event"] == "runtime"
                )
                assert (runtime["id"], runtime["status"]) == (
                    replacement,
                    "running",
                )
                response = run_cells(
                    client,
                    notebook_id,
                    {cell_id: "x = 43"},
                    headers=headers("new"),
                )
                assert response.ok, response.problem
                receive_completed_run(ws, response.data["id"])
        finally:
            client.portal.call(bridge.close)
