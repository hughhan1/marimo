# Copyright 2026 Marimo. All rights reserved.
"""The editor's tools at `/api/mcp`, as the MCP SDK's stateless transport serves them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from marimo._server.api.tools import contract
from marimo._session.model import SessionMode
from tests._server.mocks import get_session_manager, token_header
from tests.mcp_request import (
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
        (ROOT / "packages/openapi/holon-contract.json").read_text()
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


def test_tools_are_for_apps_not_models(client: TestClient) -> None:
    tools = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[
        "result"
    ]["tools"]
    assert {tool["_meta"]["ui"]["visibility"][0] for tool in tools} == {"app"}
    assert all(tool["description"] for tool in tools)
    by_name = {tool["name"]: tool for tool in tools}
    # A tool's arguments are its request's fields, under their wire names,
    # and nothing else.
    arguments = by_name["kernel_set_ui_element_value"]["inputSchema"]
    assert arguments["required"] == ["objectIds", "values"]
    assert arguments["additionalProperties"] is False
    assert "outputSchema" in by_name["kernel_set_ui_element_value"]
    # A record's id is an argument before the request's fields.
    assert by_name["pair_notebooks_attachments_detach"]["inputSchema"][
        "required"
    ] == ["id", "attachment_id"]
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
    client: TestClient,
) -> None:
    # The SDK also answers the revisions before 2026-07-28, as one event of
    # a stream. The caller's credential and the reply's shape are marimo's,
    # so they hold there too.
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
            "Object missing required field `id`",
        ),
        (
            "pair_notebooks_attachments_detach",
            {"id": None, "attachment_id": "x"},
            "Expected `str`, got `null` - at `$.id`",
        ),
        (
            "version",
            {"extra": True},
            "Object contains unknown field `extra`",
        ),
        # The server fills the caller's HTTP request in; no caller sends it.
        (
            "kernel_set_ui_element_value",
            {"objectIds": [], "values": [], "request": {}},
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


def test_read_only_caller_cannot_call_an_edit_tool(client: TestClient) -> None:
    get_session_manager(client).mode = SessionMode.RUN
    result = _call(client, "kernel_format", {"codes": {}, "lineLength": 80})
    assert result["isError"] is True
    problem = tool_problem(result)
    assert problem is not None
    assert problem["status"] == 401
    assert problem["type"] == PROBLEM_KIND + "unauthorized"
