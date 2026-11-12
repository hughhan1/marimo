# Copyright 2026 Marimo. All rights reserved.
"""Let tests call a host operation by the route the protocol gives it.

The host serves its operations only as tools at `/mcp`. A request to an
operation's route becomes a `tools/call` of the operation, whose arguments
are the route's ids, the `Idempotency-Key` and `If-Match` headers and the
body's members, and the result becomes the response the route describes: a
success has the operation's status and the reply as its JSON, and a failure
has its problem's status and the problem as its body.
"""

from __future__ import annotations

import json
import re
from typing import Any

import msgspec

from marimo._server.host.tools import tools
from tests.mcp_request import request_headers, tool_payload

_ROUTES = (
    ("notebook.create", "POST", "/notebooks", 201),
    ("notebook.update", "PATCH", "/notebooks/{notebook_id}", 200),
    ("notebook.delete", "DELETE", "/notebooks/{notebook_id}", 204),
    ("notebook.open", "POST", "/notebooks/{notebook_id}/open", 200),
    ("notebook.edit", "POST", "/notebooks/{notebook_id}/edits", 200),
    ("notebook.save", "POST", "/notebooks/{notebook_id}/save", 200),
    ("runtime.start", "PUT", "/notebooks/{notebook_id}/runtime", 202),
    ("runtime.stop", "DELETE", "/notebooks/{notebook_id}/runtime", 202),
    (
        "runtime.restart",
        "POST",
        "/notebooks/{notebook_id}/runtime/restart",
        202,
    ),
    ("runtime.run", "POST", "/notebooks/{notebook_id}/runtime/runs", 202),
    (
        "runtime.execute",
        "POST",
        "/notebooks/{notebook_id}/runtime/execute",
        202,
    ),
    (
        "runtime.interrupt",
        "POST",
        "/notebooks/{notebook_id}/runtime/interrupt",
        202,
    ),
    (
        "runtime.cancel",
        "DELETE",
        "/notebooks/{notebook_id}/runtime/runs/{run_id}",
        202,
    ),
    (
        "runtime.input",
        "POST",
        "/notebooks/{notebook_id}/runtime/inputs/{input_id}/reply",
        200,
    ),
)
_PATTERNS = [
    (
        name,
        method,
        re.compile(re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "/?"),
        status,
    )
    for name, method, path, status in _ROUTES
]
_SUCCESS = {name: status for name, _, _, status in _ROUTES}


def success_status(name: str, reply: Any) -> int:
    """The status an operation's route answers a successful reply with."""
    if (
        name == "runtime.start"
        and isinstance(reply, dict)
        and reply.get("status") != "starting"
    ):
        # The route answered a runtime it found, rather than started, with a 200.
        return 200
    return _SUCCESS[name]


_ARGUMENT_HEADERS = {
    "idempotency-key": "idempotency_key",
    "if-match": "if_match",
}


class ToolRequest(msgspec.Struct, frozen=True):
    """An operation's route, as the tool call that replaces it."""

    name: str
    status: int
    """The status the route answers a success with."""
    body: bytes
    """The JSON-RPC request."""
    headers: dict[str, str]
    """The request's headers, without those that became arguments."""


def tool_request(
    method: str, path: str, headers: dict[str, str], body: bytes
) -> ToolRequest | None:
    """The call that replaces a request to `path`, below the host API's prefix."""
    for name, route_method, pattern, status in _PATTERNS:
        match = pattern.fullmatch(path)
        if match is None or method.upper() != route_method:
            continue
        arguments: Any = dict(match.groupdict())
        remaining: dict[str, str] = {}
        for header, value in headers.items():
            argument = _ARGUMENT_HEADERS.get(header.lower())
            if argument is None:
                remaining[header] = value
            else:
                arguments[argument] = value
        if body:
            try:
                members = json.loads(body)
            except ValueError:
                members = body.decode(errors="replace")
            # Not an object: sent as it is, for the host to refuse.
            arguments = (
                {**arguments, **members}
                if isinstance(members, dict)
                else members
            )
        return ToolRequest(
            name,
            status,
            json.dumps(tool_payload(name, arguments)).encode(),
            {**remaining, **request_headers("tools/call", name)},
        )
    return None


def route_response(
    call: ToolRequest, status: int, body: bytes
) -> tuple[int, str | None, bytes]:
    """The status, media type and body a route would have answered with."""
    try:
        message = json.loads(body)
    except ValueError:
        return status, None, body
    if status != 200 or "result" not in message:
        return status, "application/json", body
    result = message["result"]
    if result.get("isError"):
        problem = json.loads(result["content"][0]["text"])
        return (
            problem["status"],
            "application/problem+json",
            json.dumps(problem).encode(),
        )
    reply = result.get("structuredContent") or {}
    # The reply must be what the tool declares, as a generated client checks.
    output = tools()[call.name].output
    if output is None:
        return call.status, None, b""
    msgspec.convert(reply, output)
    return (
        success_status(call.name, reply),
        "application/json",
        json.dumps(reply).encode(),
    )
