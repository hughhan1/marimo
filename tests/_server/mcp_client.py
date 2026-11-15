# Copyright 2026 Marimo. All rights reserved.
"""Let tests call a tool by the path of the route it replaced.

`client.post("/api/kernel/rename", json=body)` becomes a `tools/call` request
to `/api/mcp` whose arguments are the body's fields, and the result becomes an
ordinary response: a success is a 200 whose JSON is the structured result,
and a failure has its status and `{"detail": ...}` as its body. A path that
names no tool is sent as it is.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import msgspec
from starlette.testclient import TestClient

from marimo._server.api.router import build_routes  # noqa: F401
from marimo._server.api.tools import TOOLS

PROTOCOL_VERSION = "2026-07-28"


def request_headers(name: str) -> dict[str, str]:
    """The headers a call to the tool `name` carries."""
    return {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": name,
    }


def tool_payload(name: str, arguments: Any) -> dict[str, Any]:
    """A `tools/call` request for the tool `name`."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "tests",
                    "version": "0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def _tool(url: str) -> str | None:
    path = urlsplit(url).path
    start = path.find("/api/")
    if start == -1:
        return None
    name = re.sub(r"\W+", "_", path[start:].removeprefix("/api/")).strip("_")
    return name if name in TOOLS else None


def _response(
    outer: httpx.Response, name: str, request: httpx.Request
) -> httpx.Response:
    if outer.status_code != 200 or "result" not in outer.json():
        return outer
    result = outer.json()["result"]
    if result.get("isError"):
        failure = json.loads(result["content"][0]["text"])
        status = failure.pop("status")
        body = json.dumps(failure).encode()
    else:
        status = 200
        structured = result.get("structuredContent") or {}
        # The reply must be what the tool declares, as a generated client
        # checks before handing it to the caller.
        if (output := TOOLS[name].output) is not None:
            msgspec.convert(structured, output)
        body = json.dumps(structured).encode()
    return httpx.Response(
        status,
        headers=[
            ("content-type", "application/json"),
            *(
                ("set-cookie", value)
                for value in outer.headers.get_list("set-cookie")
            ),
        ],
        content=body,
        request=request,
    )


_original_request = TestClient.request


def _request(self: TestClient, method: str, url: Any, **kwargs: Any) -> Any:
    full = self._merge_url(url)
    name = _tool(str(full))
    if name is None:
        return _original_request(self, method, url, **kwargs)
    # The URL's query is the request's, such as a credential: it stays on
    # the URL of the call.
    query = dict(kwargs.pop("params", None) or {})
    query.update(
        pair.split("=", 1)
        for pair in urlsplit(str(url)).query.split("&")
        if "=" in pair
    )
    arguments = kwargs.pop("json", None)
    raw = kwargs.pop("content", None)
    if raw is not None:
        try:
            arguments = json.loads(raw)
        except ValueError:
            arguments = raw.decode() if isinstance(raw, bytes) else raw
    form = kwargs.pop("data", None)
    if form is not None:
        arguments = dict(form)
    headers = dict(kwargs.pop("headers", None) or {})
    prefix = full.path.split("/api/")[0]
    outer = _original_request(
        self,
        "POST",
        f"{full.scheme}://{full.netloc.decode()}{prefix}/api/mcp"
        + (f"?{urlencode(query)}" if query else ""),
        json=tool_payload(name, {} if arguments is None else arguments),
        headers={**request_headers(name), **headers},
        **kwargs,
    )
    return _response(outer, name, outer.request)


def install() -> None:
    TestClient.request = _request  # type: ignore[method-assign]
