# Copyright 2026 Marimo. All rights reserved.
"""App notebook identity and the boundary between sessions and discovery."""

from __future__ import annotations

from marimo._server.host.bridge import SessionBridge
from marimo._server.host.context import HostContext
from marimo._server.main import create_starlette_app
from marimo._session.model import SessionMode
from tests._server.host.drive import TOKEN, call, refused
from tests._server.mocks import (
    get_mock_session_manager,
    get_starlette_server_state_init,
)


async def test_app_mode_never_exposes_the_catalog_or_mutations() -> None:
    manager = get_mock_session_manager(mode=SessionMode.RUN)
    app = create_starlette_app(base_url="", enable_auth=False)
    get_starlette_server_state_init(session_manager=manager).apply(app.state)
    bridge = SessionBridge(manager)
    app.state.host_context = HostContext(bridge=bridge, token=TOKEN, port=1234)
    await bridge.open()
    try:
        assert (await refused(app, "catalog"))["status"] == 404
        edited = await call(
            app,
            "notebook.edit",
            idempotency_key="edit",
            notebook_id="unknown",
            changes=[],
        )
        assert edited.problem["status"] == 404
        assert edited.problem["type"].endswith(":not-found")
    finally:
        bridge.close()
