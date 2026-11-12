# Copyright 2026 Marimo. All rights reserved.
"""The host protocol's tools, as the host and the editor serve them."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from marimo._core.calls import HANDLERS
from marimo._core.protocol_tools import TOOLS
from marimo._server.api.tools import TOOLS as EDITOR_TOOLS
from marimo._server.host.api import CATALOG
from marimo._server.host.bridge import SessionBridge
from marimo._server.host.context import HostContext
from marimo._server.main import create_starlette_app
from tests._server.host.drive import AUTH, PORT, TOKEN, finished
from tests._server.mocks import (
    get_mock_session_manager,
    get_starlette_server_state_init,
    token_header,
)
from tests.mcp_request import request_headers, request_meta

if TYPE_CHECKING:
    from starlette.testclient import TestClient

LIST: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {"_meta": request_meta()},
}


def listed(response: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in response["result"]["tools"]}


def test_every_tool_has_one_handler() -> None:
    assert HANDLERS.keys() | CATALOG.keys() == TOOLS.keys()
    assert not HANDLERS.keys() & CATALOG.keys()


async def test_the_host_lists_every_tool() -> None:
    manager = get_mock_session_manager()
    app = create_starlette_app(base_url="", enable_auth=True)
    get_starlette_server_state_init(session_manager=manager).apply(app.state)
    bridge = SessionBridge(manager)
    app.state.host_context = HostContext(bridge=bridge, token=TOKEN, port=PORT)
    try:
        response = await finished(
            app,
            "POST",
            "/mcp",
            headers={**AUTH, **request_headers("tools/list")},
            body=json.dumps(LIST).encode(),
        )
    finally:
        bridge.close()
    assert listed(response.json()) == TOOLS.keys()


def test_the_editor_lists_the_notebook_tools_beside_its_own(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/mcp",
        json=LIST,
        headers={
            **token_header("fake-token"),
            **request_headers("tools/list"),
        },
    )
    names = listed(response.json())
    assert names & TOOLS.keys() == HANDLERS.keys()
    assert names - HANDLERS.keys() <= EDITOR_TOOLS.keys()
