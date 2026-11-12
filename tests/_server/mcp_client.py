# Copyright 2026 Marimo. All rights reserved.
"""Let tests call the API by its operation path, through the MCP endpoint.

`client.post("/api/kernel/rename", json=body)` becomes a `tools/call` request to
`/api/mcp` whose arguments are the body's fields, and the result becomes an
ordinary response: a success is a 200 whose JSON is the structured result, a
file is a response with the file's type and bytes, and a failure has its
problem's status and `{"detail": ...}` as its body. A host operation's route
becomes a call of the host's tools, as `tests._server.host.routes` does it.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

import httpx
import msgspec
from starlette.testclient import TestClient

from marimo._server.api.router import build_routes  # noqa: F401
from marimo._server.api.tools import TOOLS
from marimo._server.host import HOST_API_PATH
from tests._server.host.routes import route_response, tool_request
from tests.mcp_request import request_headers, tool_payload

_PAIR = re.compile(
    r"/api/pair/notebooks/([^/]+)/(messages|attachments/detach)"
)
_FILE_URI = "file:///"
# A file without a filename is sent under this name.
_NO_FILENAME = "file"


def _tool_call(url: str) -> tuple[str, dict[str, str]] | None:
    path = urlsplit(url).path
    start = path.find("/api/")
    if start == -1:
        start = path.find("/auth/token")
    if start == -1:
        return None
    path = path[start:]
    pair = _PAIR.fullmatch(path)
    if pair:
        name = "pair_notebooks_" + pair.group(2).replace("/", "_")
        return name, {"id": pair.group(1)}
    name = re.sub(r"\W+", "_", path.removeprefix("/api/")).strip("_")
    return (name, {}) if name in TOOLS else None


def _response(
    outer: httpx.Response,
    name: str,
    request: httpx.Request,
    download: bool = False,
) -> httpx.Response:
    if outer.status_code != 200 or "result" not in outer.json():
        return outer
    result = outer.json()["result"]
    status = 200
    headers: dict[str, str] = {"content-type": "application/json"}
    content = result.get("content") or []
    resource = next(
        (
            item["resource"]
            for item in content
            if item.get("type") == "resource"
        ),
        None,
    )
    if result.get("isError"):
        text = content[0]["text"]
        try:
            problem = json.loads(text)
        except ValueError:
            problem = None
        if isinstance(problem, dict) and "status" in problem:
            status = problem["status"]
            detail = problem.get("detail", problem.get("title"))
            body = json.dumps({"detail": detail}).encode()
        else:
            status, body = 500, json.dumps({"detail": text}).encode()
    elif resource is not None:
        media_type = resource.get("mimeType", "application/octet-stream")
        body = base64.b64decode(resource["blob"])
        text = media_type.startswith("text/") or media_type.endswith(
            ("json", "xml", "javascript")
        )
        headers["content-type"] = (
            f"{media_type}; charset=utf-8" if text else media_type
        )
        filename = unquote(str(resource["uri"]).removeprefix(_FILE_URI))
        if filename and filename != _NO_FILENAME:
            kind = "attachment" if download or not text else "inline"
            headers["content-disposition"] = (
                f"{kind}; filename*=UTF-8''{quote(filename)}"
            )
    else:
        structured = result.get("structuredContent") or {}
        # The reply must be what the tool declares, as a generated client
        # checks before handing it to the caller.
        if (output := TOOLS[name].output) is not None:
            msgspec.convert(structured, output)
        body = json.dumps(structured).encode()
    # The call's own response may set the login cookie.
    cookies = [
        ("set-cookie", value) for value in outer.headers.get_list("set-cookie")
    ]
    return httpx.Response(
        status,
        headers=[*headers.items(), *cookies],
        content=body,
        request=request,
    )


_original_request = TestClient.request


def _host_request(
    self: TestClient, method: str, url: Any, **kwargs: Any
) -> Any:
    full = self._merge_url(url)
    prefix, found, path = full.path.partition(HOST_API_PATH)
    if not found or kwargs.get("data") is not None:
        return None
    body = kwargs.get("content") or b""
    if kwargs.get("json") is not None:
        body = json.dumps(kwargs["json"]).encode()
    headers = dict(kwargs.get("headers") or {})
    call = tool_request(method, path, headers, body)
    if call is None:
        return None
    outer = _original_request(
        self,
        "POST",
        f"{full.scheme}://{full.netloc.decode()}{prefix}{HOST_API_PATH}/mcp"
        + (f"?{full.query.decode()}" if full.query else ""),
        content=call.body,
        headers={"content-type": "application/json", **call.headers},
        **{
            key: value
            for key, value in kwargs.items()
            if key not in ("content", "json", "headers")
        },
    )
    status, media_type, content = route_response(
        call, outer.status_code, outer.content
    )
    return httpx.Response(
        status,
        headers={} if media_type is None else {"content-type": media_type},
        content=content,
        request=outer.request,
    )


def _request(self: TestClient, method: str, url: Any, **kwargs: Any) -> Any:
    host = _host_request(self, method, url, **kwargs)
    if host is not None:
        return host
    full = self._merge_url(url)
    call = _tool_call(str(full))
    if call is None:
        return _original_request(self, method, url, **kwargs)
    name, params = call
    # The URL's query is the request's, not the tool's: a credential, or the
    # file that names the session. It stays on the URL of the call.
    query = dict(kwargs.pop("params", None) or {})
    query.update(
        pair.split("=", 1)
        for pair in urlsplit(str(url)).query.split("&")
        if "=" in pair
    )
    body = kwargs.pop("json", None)
    raw = kwargs.pop("content", None)
    if raw is not None:
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode() if isinstance(raw, bytes) else raw
    form = kwargs.pop("data", None)
    if form is not None:
        body = dict(form)
    headers = dict(kwargs.pop("headers", None) or {})
    arguments: Any
    if name == "pair_notebooks_messages":
        # The message is any JSON, carried whole.
        arguments = {**params, "message": body}
    elif body is None or isinstance(body, dict):
        arguments = {**params, **(body or {})}
    else:
        # Not an object: sent as it is, for the transport to refuse.
        arguments = body
    prefix = full.path.split("/api/")[0].split("/auth/")[0]
    outer = _original_request(
        self,
        "POST",
        f"{full.scheme}://{full.netloc.decode()}{prefix}/api/mcp"
        + (f"?{urlencode(query)}" if query else ""),
        json=tool_payload(name, arguments),
        headers={**request_headers("tools/call", name), **headers},
        **kwargs,
    )
    download = isinstance(body, dict) and bool(body.get("download"))
    return _response(outer, name, outer.request, download)


def install() -> None:
    TestClient.request = _request  # type: ignore[method-assign]
