# Copyright 2026 Marimo. All rights reserved.
"""The editor's tools at `/api/mcp`, as the MCP SDK's stateless transport serves them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sse_starlette.sse import AppStatus

from marimo._server.api.tools import contract
from marimo._session.model import SessionMode
from tests._server.api.endpoints.ws_helpers import receive_until
from tests._server.mcp_client import call_tool
from tests._server.mocks import (
    get_session_manager,
    get_single_notebook_id,
    token_header,
    with_session,
)
from tests._server.session_events import connect_session
from tests.mcp_request import (
    Called,
    request_headers,
    request_meta,
    tool_payload,
    tool_problem,
)

if TYPE_CHECKING:
    from starlette.testclient import TestClient

HEADERS = {"Marimo-Session-Id": "123", **token_header("fake-token")}
ROOT = Path(__file__).parents[3]
PROBLEM_KIND = "urn:marimo:problem:"
SESSION_ID = "session-123"
ASSISTANT_TOOLS = {
    "get_active_notebooks",
    "get_cell_dependency_graph",
    "get_cell_outputs",
    "get_cell_runtime_data",
    "get_database_tables",
    "get_lightweight_cell_map",
    "get_marimo_rules",
    "get_notebook_errors",
    "get_tables_and_variables",
    "lint_notebook",
}


def _rpc(
    client: TestClient,
    payload: dict[str, Any],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Post a request as a client that follows the transport does."""
    params = payload.setdefault("params", {})
    params.setdefault("_meta", request_meta())
    sent = {
        **HEADERS,
        **request_headers(payload["method"], params.get("name")),
        **(headers or {}),
    }
    response = client.post("/api/mcp", json=payload, headers=sent)
    assert response.status_code == status, response.text
    return response.json()  # type: ignore[no-any-return]


def _call(
    client: TestClient, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = _rpc(client, tool_payload(name, arguments))[
        "result"
    ]
    return result


def test_the_committed_contract_matches_the_server() -> None:
    # Regenerate with `make fe-codegen`.
    committed = json.loads(
        (ROOT / "frontend/server/holon-contract.json").read_text()
    )
    assert committed["schema"]["tools"] == [
        {
            key: tool[key]
            for key in ("name", "inputSchema", "outputSchema")
            if key in tool
        }
        for tool in json.loads(json.dumps(contract()))
    ]


def test_discover_names_the_protocol_and_server(client: TestClient) -> None:
    result = _rpc(
        client, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
    )["result"]
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == (
        "marimo"
    )


def test_the_assistants_tools_are_for_models_and_the_rest_for_apps(
    client: TestClient,
) -> None:
    tools = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[
        "result"
    ]["tools"]
    for_models = [tool for tool in tools if tool["name"] in ASSISTANT_TOOLS]
    for_apps = [tool for tool in tools if tool["name"] not in ASSISTANT_TOOLS]
    assert {tool["name"] for tool in for_models} == ASSISTANT_TOOLS
    assert all("_meta" not in tool for tool in for_models)
    assert {tool["_meta"]["ui"]["visibility"][0] for tool in for_apps} == {
        "app"
    }
    assert all(tool["description"] for tool in tools)
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["get_cell_outputs"]["inputSchema"]["required"] == [
        "session_id"
    ]
    # A tool's arguments are its request's fields, under their wire names,
    # after the notebook it acts on, and nothing else.
    arguments = by_name["kernel_set_ui_element_value"]["inputSchema"]
    assert arguments["required"] == ["notebook_id", "objectIds", "values"]
    assert arguments["additionalProperties"] is False
    assert "outputSchema" in by_name["kernel_set_ui_element_value"]
    # A tool for the whole server names no notebook, and one that works with
    # or without a notebook takes it optionally.
    assert (
        "notebook_id"
        not in by_name["files_file_details"]["inputSchema"]["properties"]
    )
    optional = by_name["kernel_save_user_config"]["inputSchema"]
    assert "notebook_id" in optional["properties"]
    assert "notebook_id" not in optional["required"]
    # A file is an embedded resource, which no output schema describes.
    assert "outputSchema" not in by_name["export_html"]


def test_unknown_method_and_tool_are_protocol_errors(
    client: TestClient,
) -> None:
    method = _rpc(
        client, {"jsonrpc": "2.0", "id": 1, "method": "nope"}, status=404
    )
    assert method["error"]["code"] == -32601
    # An unknown tool is invalid params, which the transport sends as a 400.
    tool = _rpc(client, tool_payload("nope"), status=400)
    assert tool["error"]["code"] == -32602


def test_invalid_json_is_a_parse_error(client: TestClient) -> None:
    response = client.post(
        "/api/mcp",
        content=b"{",
        headers={
            **HEADERS,
            **request_headers("tools/list"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_the_body_must_be_json_the_caller_accepts(client: TestClient) -> None:
    # The transport reads JSON only, and answers only a caller that takes it.
    response = client.post(
        "/api/mcp",
        content=b"{}",
        headers={**HEADERS, **request_headers("tools/list")},
    )
    assert response.status_code == 400
    assert "Content-Type" in response.text
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            **HEADERS,
            **request_headers("tools/list"),
            "Accept": "text/html",
        },
    )
    assert response.status_code == 406


@pytest.mark.parametrize(
    ("case", "headers", "payload", "meta", "status", "code"),
    [
        (
            "a browser on another origin",
            {"origin": "https://attacker.example"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            True,
            403,
            -32600,
        ),
        (
            "a method header that disagrees with the body",
            {"Mcp-Method": "server/discover"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            True,
            400,
            -32020,
        ),
        (
            "a tool header that disagrees with the body",
            {"Mcp-Name": "usage"},
            tool_payload("lsp_health"),
            True,
            400,
            -32020,
        ),
        (
            "no request metadata",
            {},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            False,
            400,
            -32602,
        ),
        (
            "a null id",
            {},
            {"jsonrpc": "2.0", "id": None, "method": "tools/list"},
            True,
            400,
            -32600,
        ),
        (
            "arguments that are not an object",
            {},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "version", "arguments": "{"},
            },
            True,
            400,
            -32602,
        ),
    ],
)
def test_the_transport_refuses(
    client: TestClient,
    case: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    meta: bool,
    status: int,
    code: int,
) -> None:
    del case
    params = payload.setdefault("params", {})
    if meta:
        params.setdefault("_meta", request_meta())
    sent = {
        **HEADERS,
        **request_headers(payload["method"], params.get("name")),
        **headers,
    }
    response = client.post("/api/mcp", json=payload, headers=sent)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_a_notification_is_acknowledged_and_dropped(
    client: TestClient,
) -> None:
    # The protocol has no client notifications over HTTP; one is accepted
    # with no body, as the transport does, rather than answered with an error.
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**HEADERS, **request_headers("notifications/initialized")},
    )
    assert response.status_code == 202
    assert response.content == b""


def test_an_older_client_is_answered_under_the_same_rules(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK also answers the revisions before 2026-07-28, as one event of
    # a stream. The caller's credential and the reply's shape are marimo's,
    # so they hold there too. A server shut down earlier in this process
    # sets the SSE library's process-wide exit flag, which would end the
    # stream at once, as it does when the process exits.
    monkeypatch.setattr(AppStatus, "should_exit", False)

    def reply(response: Any) -> dict[str, Any]:
        _, _, data = response.text.partition("data: ")
        return json.loads(data)  # type: ignore[no-any-return]

    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "status", "arguments": {}},
    }
    older = {"MCP-Protocol-Version": "2025-06-18"}
    refused = client.post("/api/mcp", json=call, headers=older)
    problem = tool_problem(reply(refused)["result"])
    assert problem is not None
    assert problem["status"] == 401
    answered = client.post("/api/mcp", json=call, headers={**HEADERS, **older})
    assert reply(answered)["result"]["structuredContent"]["status"] == (
        "healthy"
    )


def test_the_same_origin_and_an_encoded_tool_name_are_accepted(
    client: TestClient,
) -> None:
    # A browser on the server's own origin, naming the tool the way the
    # transport encodes names that are not header-safe.
    result = _rpc(
        client,
        tool_payload("usage"),
        headers={
            "host": "localhost",
            "origin": "http://localhost",
            "Mcp-Name": "=?base64?dXNhZ2U=?=",
        },
    )["result"]
    assert result["isError"] is False
    assert {"memory", "cpu"} <= result["structuredContent"].keys()


def test_only_post_is_served(client: TestClient) -> None:
    # The route takes POST alone; another method falls through to the app's
    # catch-all, as any unknown path does.
    response = client.get("/api/mcp", headers=HEADERS)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("name", "arguments", "detail"),
    [
        (
            "files_file_details",
            {"path": "/x", "bogus": 1},
            "Object contains unknown field `bogus`",
        ),
        (
            "files_file_details",
            {},
            "Object missing required field `path`",
        ),
        (
            "files_file_details",
            {"path": 1},
            "Expected `str`, got `int` - at `$.path`",
        ),
        (
            "pair_notebooks_attachments_detach",
            {"attachment_id": "x"},
            "Object missing required field `notebook_id`",
        ),
        (
            "pair_notebooks_attachments_detach",
            {"notebook_id": None, "attachment_id": "x"},
            "Expected `str`, got `null` - at `$.notebook_id`",
        ),
        (
            "version",
            {"extra": True},
            "Object contains unknown field `extra`",
        ),
        # The server fills the caller's HTTP request in; no caller sends it.
        (
            "kernel_set_ui_element_value",
            {
                "notebook_id": "nb-unknown",
                "objectIds": [],
                "values": [],
                "request": {},
            },
            "Object contains unknown field `request`",
        ),
    ],
)
def test_arguments_are_validated_before_the_handler_runs(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
    detail: str,
) -> None:
    result = _call(client, name, arguments)
    assert result["isError"] is True
    problem = tool_problem(result)
    assert problem == {
        "status": 400,
        "title": "Invalid request",
        "type": PROBLEM_KIND + "invalid-request",
        "detail": detail,
    }
    # A failed call has no structured result to fit the output schema.
    assert "structuredContent" not in result


def test_a_reply_is_the_structured_result(client: TestClient) -> None:
    result = _call(client, "status_connections")
    assert result["isError"] is False
    assert result["structuredContent"] == {"active": 0}
    assert result["content"] == [
        {"type": "text", "text": "status_connections: ok"}
    ]


def test_tool_failure_is_a_problem_with_its_status(
    client: TestClient,
) -> None:
    result = _call(client, "files_file_details", {"path": "/not/a/real/path"})
    assert result["isError"] is True
    problem = tool_problem(result)
    assert problem is not None
    assert problem["status"] == 500
    assert problem["title"] == "Internal error"
    assert problem["type"] == PROBLEM_KIND + "internal"
    assert "/not/a/real/path" in problem["detail"]
    assert "structuredContent" not in result


def test_a_call_of_any_size_reaches_its_tool(client: TestClient) -> None:
    # Larger than the SDK's default cap, as a file in a UI element's value is.
    path = "/" + "x" * (5 * 1024 * 1024)
    problem = tool_problem(_call(client, "files_file_details", {"path": path}))
    assert problem is not None
    assert problem["type"] == PROBLEM_KIND + "internal"


def test_a_raised_status_is_the_problems(client: TestClient) -> None:
    problem = tool_problem(_call(client, "secrets_delete", {"key": "x"}))
    assert problem == {
        "status": 501,
        "title": "Unsupported operation",
        "type": PROBLEM_KIND + "unsupported-operation",
        "detail": "Not implemented",
    }


def test_tool_call_needs_a_credential(client: TestClient) -> None:
    response = client.post(
        "/api/mcp",
        json=tool_payload("usage"),
        headers={
            "Marimo-Session-Id": "123",
            **request_headers("tools/call", "usage"),
        },
    )
    # The transport admits the call; the tool refuses it, as a problem.
    assert response.status_code == 200
    problem = tool_problem(response.json()["result"])
    assert problem is not None
    assert problem["status"] == 401
    assert problem["type"] == PROBLEM_KIND + "unauthorized"


@pytest.mark.parametrize(
    ("name", "arguments", "status", "kind"),
    [
        ("home_running_notebooks", {}, 401, "unauthorized"),
        ("get_active_notebooks", {}, 403, "forbidden"),
    ],
)
def test_read_only_caller_cannot_call_an_edit_tool(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
    status: int,
    kind: str,
) -> None:
    get_session_manager(client).mode = SessionMode.RUN
    problem = tool_problem(_call(client, name, arguments))
    assert problem is not None
    assert problem["status"] == status
    assert problem["type"] == PROBLEM_KIND + kind


@with_session(SESSION_ID)
def test_an_assistant_tool_answers_with_its_result_as_text_too(
    client: TestClient,
) -> None:
    result = _call(client, "get_active_notebooks")
    assert result["isError"] is False
    notebooks = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == notebooks
    (notebook,) = notebooks["data"]["notebooks"]
    assert notebook["session_id"] == SESSION_ID

    cells = _call(
        client, "get_lightweight_cell_map", {"session_id": SESSION_ID}
    )["structuredContent"]
    assert cells["session_id"] == SESSION_ID
    assert cells["total_cells"] == 1


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"session_id": "nope"}, "SESSION_NOT_FOUND"),
        ({"preview_lines": 1}, "BAD_ARGUMENTS"),
    ],
)
def test_an_assistant_tool_failure_says_what_went_wrong(
    client: TestClient, arguments: dict[str, Any], code: str
) -> None:
    result = _call(client, "get_lightweight_cell_map", arguments)
    assert result["isError"] is True
    assert "structuredContent" not in result
    prefix = "Error executing tool get_lightweight_cell_map: "
    text = result["content"][0]["text"]
    assert text.startswith(prefix)
    assert json.loads(text.removeprefix(prefix))["code"] == code


def test_a_caller_without_a_tab_names_the_notebook(client: TestClient) -> None:
    token = token_header("fake-token")
    with connect_session(client, "/ws?session_id=page", headers=token) as page:
        receive_until("kernel-ready", page)
        notebook_id = get_single_notebook_id(client)

        assert call_tool(
            client, "kernel_status", headers=token, notebook_id=notebook_id
        ).data == {"state": "idle"}
        unknown = call_tool(
            client, "kernel_status", headers=token, notebook_id="nb-unknown"
        )
        assert unknown.problem["type"] == PROBLEM_KIND + "not-found"


def test_a_second_tab_edits_after_taking_over(client: TestClient) -> None:
    token = token_header("fake-token")
    with (
        connect_session(client, "/ws?session_id=first", headers=token) as a,
        connect_session(client, "/ws?session_id=second", headers=token) as b,
    ):
        receive_until("kernel-ready", a)
        receive_until("kernel-ready", b)
        notebook_id = get_single_notebook_id(client)
        second = {**token, "Marimo-Session-Id": "second"}

        def run() -> Called:
            return call_tool(
                client,
                "kernel_scratchpad_run",
                headers=second,
                notebook_id=notebook_id,
                code="x = 1",
            )

        assert run().problem["type"] == PROBLEM_KIND + "forbidden"
        assert call_tool(
            client, "kernel_takeover", headers=second, notebook_id=notebook_id
        ).ok
        assert run().ok
