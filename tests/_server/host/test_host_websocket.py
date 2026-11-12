# Copyright 2026 Marimo. All rights reserved.
"""The notebook event of a notebook whose session is already running."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import msgspec
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware

from marimo._messaging.cell_output import CellChannel, CellOutput
from marimo._messaging.notification import (
    CellNotification,
    CompletedRunNotification,
)
from marimo._server.host.bridge import SessionBridge
from marimo._server.host.context import HostContext
from marimo._server.host.middleware import HostApiMiddleware
from marimo._server.models.home import MarimoFile
from marimo._server.workspace import FixedFilesWorkspace
from marimo._session.notebook import AppFileManager
from marimo._session.state.session_view import SessionView
from tests._server.host.conftest import FakeManager, FakeSession
from tests._server.host.drive import (
    PORT,
    TOKEN,
    position,
    subscribe,
    until_ready,
    wait_for,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from tests._server.host.drive import Response


@pytest.fixture
async def session_host(
    manager: FakeManager, tmp_path: Path
) -> AsyncIterator[tuple[Starlette, SessionBridge, FakeSession]]:
    path = tmp_path / "notebook.py"
    path.write_text(
        "import marimo\napp = marimo.App()\n"
        "@app.cell\ndef _():\n    x = 1\n    return (x,)\n"
    )
    manager.workspace = FixedFilesWorkspace(
        [MarimoFile(name=path.name, path=str(path))], directory=str(tmp_path)
    )
    session = FakeSession(str(path))
    session.document = AppFileManager(str(path)).app.cell_manager.document
    session.session_view = SessionView()
    session.session_view.add_notification(
        CellNotification(
            cell_id=session.document.cell_ids[0],
            output=CellOutput(
                channel=CellChannel.OUTPUT,
                mimetype="text/plain",
                data="retained output",
                timestamp=1.0,
            ),
            status="idle",
            run_id="r1",
            timestamp=1.0,
        )
    )
    manager.sessions["existing"] = session
    bridge = SessionBridge(manager)  # type: ignore[arg-type]
    app = Starlette(middleware=[Middleware(HostApiMiddleware)])
    app.state.host_context = HostContext(bridge=bridge, token=TOKEN, port=PORT)
    await bridge.open()
    try:
        yield app, bridge, session
    finally:
        bridge.close()


async def follow(
    app: Starlette, **arguments: Any
) -> tuple[Response, Callable[[], Awaitable[None]]]:
    """Subscribe to the session's notebook through `ready`, with its end."""
    (notebook_id,) = app.state.host_context.bridge.host.notebooks
    response, task, disconnect = subscribe(
        app, "notebook", notebook_id=notebook_id, **arguments
    )
    await wait_for(response, until_ready)

    async def end() -> None:
        disconnect()
        await asyncio.wait_for(task, 5)

    return response, end


async def test_the_snapshot_carries_the_session_document_before_ready(
    session_host: tuple[Starlette, SessionBridge, FakeSession],
) -> None:
    app, _, session = session_host
    response, end = await follow(app)
    await end()
    events = response.records
    names = response.names()
    assert names[0] == "notebook"
    assert names.index("cell") < names.index("document")
    assert "attachment" in names
    assert names[-1] == "ready"
    # Session state reaches editors through their own `session` event.
    assert "marimo" not in names
    assert all("id" not in event for event in events[:-1])
    cell = next(e["data"]["cell"] for e in events if e["event"] == "cell")
    document = next(
        e["data"]["document"] for e in events if e["event"] == "document"
    )
    assert cell["id"] == session.document.cell_ids[0]
    assert document["order"] == [cell["id"]]


async def test_resuming_replays_original_events_then_continues_live(
    session_host: tuple[Starlette, SessionBridge, FakeSession],
) -> None:
    app, bridge, session = session_host
    first, end = await follow(app, types=["marimo"])
    cursor = first.records[-1]["id"]
    session.notify(CompletedRunNotification(run_id="missed"))
    await wait_for(first, lambda r: len(r.records) == 2)
    await end()

    second, end = await follow(app, types=["marimo"], cursor=cursor)
    replay = list(second.records)
    assert replay[:-1] == first.records[1:]
    session.notify(CompletedRunNotification(run_id="live"))
    await wait_for(second, lambda r: len(r.records) == 3)
    await end()
    live = second.records[-1]
    assert live["data"]["marimo"]["run_id"] == "live"
    assert position(bridge.host, live["id"]) > position(
        bridge.host, replay[-1]["id"]
    )


async def test_an_expired_cursor_resets_to_current_state(
    session_host: tuple[Starlette, SessionBridge, FakeSession],
) -> None:
    app, bridge, _ = session_host
    (notebook_id,) = bridge.host.notebooks
    notebook = await bridge.host.notebook(notebook_id)
    types = ["cell", "document", "marimo", "custom"]
    first, end = await follow(app, types=types)
    await end()
    initial = first.records
    cursor = initial[-1]["id"]
    # Expire the default 1000-event replay history.
    for index in range(1001):
        notebook.emit("test.expire", {"index": index})

    second, end = await follow(app, types=types, cursor=cursor)
    reset = list(second.records)
    assert reset[0] == {"event": "reset", "data": {}}
    assert reset[1:-1] == initial[:-1]
    assert position(bridge.host, reset[-1]["id"]) > position(
        bridge.host, cursor
    )
    event = notebook.emit("test.live", {})
    await wait_for(second, lambda r: len(r.records) > len(reset))
    await end()
    live = second.records[-1]
    assert live["event"] == "custom"
    assert live["data"]["custom"] == msgspec.to_builtins(event)
    assert position(bridge.host, live["id"]) > position(
        bridge.host, reset[-1]["id"]
    )
