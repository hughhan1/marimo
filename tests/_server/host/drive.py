# Copyright 2026 Marimo. All rights reserved.
"""ASGI test requests with incremental access to event streams.

Direct ASGI requests let tests read events while a response remains open, and
call the host's tools at its `/mcp`.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import msgspec

from marimo._core.protocol_tools import TOOLS
from marimo._server.host import HOST_API_PATH
from tests.mcp_request import Called, called, request_headers, tool_payload

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    from starlette.applications import Starlette
    from starlette.testclient import WebSocketTestSession
    from starlette.types import ASGIApp, Receive, Scope, Send

    from marimo._core.host import Host
    from tests._server.session_events import SessionEvents

PORT = 1234
"""Server port used to validate the request's Host header."""

AUTHORITY = f"127.0.0.1:{PORT}"
LOOPBACK = ("127.0.0.1", 50000)
TOKEN = "host-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def with_client_address(
    app: ASGIApp, address: tuple[str, int] = LOOPBACK
) -> ASGIApp:
    """Set the peer address without requiring Starlette's newer `client` option."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": address}
        await app(scope, receive, send)

    return wrapped


def position(host: Host, cursor: str) -> int:
    """Return where a cursor the host issued falls in its stream."""
    found = host._stream.position(cursor)
    assert found is not None, f"{cursor} is not from this host"
    return found


def socket_snapshot(
    websocket: WebSocketTestSession | SessionEvents,
) -> list[dict[str, Any]]:
    """Read the opening snapshot or replay, including its ready boundary."""
    events = []
    while True:
        event = websocket.receive_json()
        events.append(event)
        if event["event"] == "ready":
            assert event["data"] == {}
            assert isinstance(event["id"], str)
            return events


class Response:
    def __init__(self) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self.records: list[dict[str, Any]] = []
        self.body = b""
        self.changed = asyncio.Event()
        self._buffer = ""

    def feed(self, chunk: bytes) -> None:
        self.body += chunk
        self._buffer += chunk.decode()
        while "\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n\n", 1)
            self.records.append(parse(raw))
        self.changed.set()

    def json(self) -> Any:
        return json.loads(self.body)

    def names(self) -> list[str]:
        return [r["event"] for r in self.records if "event" in r]


def parse(raw: str) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for line in raw.split("\n"):
        if line.startswith(":"):
            record["comment"] = line[1:].strip()
        elif ":" in line:
            field, _, value = line.partition(":")
            record[field] = value.strip()
    if "data" in record:
        record["data"] = json.loads(record["data"])
    return record


def request(
    app: Starlette,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: str = "",
    client: tuple[str, int] = LOOPBACK,
    host: str = AUTHORITY,
    prefix: str = HOST_API_PATH,
) -> tuple[Response, asyncio.Task[None], Callable[[], None]]:
    """Return the response, request task, and disconnect callback.

    `path` is relative to `prefix`, the host API's by default.
    """
    headers = dict(headers or {})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": prefix + path,
        "raw_path": (prefix + path).encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (b"host", host.encode()),
            (b"content-type", b"application/json"),
            *((k.lower().encode(), v.encode()) for k, v in headers.items()),
        ],
        "client": client,
        "server": ("127.0.0.1", PORT),
        "state": {},
    }
    response = Response()
    receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    receive_queue.put_nowait(
        {"type": "http.request", "body": body, "more_body": False}
    )

    async def receive() -> dict[str, Any]:
        return await receive_queue.get()

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = {
                k.decode(): v.decode() for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response.feed(message.get("body", b""))

    task = asyncio.get_running_loop().create_task(app(scope, receive, send))

    def disconnect() -> None:
        receive_queue.put_nowait({"type": "http.disconnect"})

    return response, task, disconnect


async def finished(
    app: Starlette,
    method: str,
    path: str,
    *,
    timeout: float = 5,
    **kwargs: Any,
) -> Response:
    """Await a complete response to a non-streaming request."""
    response, task, _ = request(app, method, path, **kwargs)
    await asyncio.wait_for(task, timeout)
    return response


async def call(
    app: Starlette,
    name: str,
    /,
    *,
    headers: dict[str, str] = AUTH,
    host: str = AUTHORITY,
    query: str = "",
    url: str = f"{HOST_API_PATH}/mcp",
    timeout: float = 5,
    **arguments: Any,
) -> Called:
    """Call a tool at `url`, the host's tools by default, as a client does.

    A success's result must be what the tool declares, as a generated client
    checks before handing it on.
    """
    response = await finished(
        app,
        "POST",
        url,
        prefix="",
        headers={**request_headers("tools/call", name), **headers},
        body=json.dumps(tool_payload(name, arguments)).encode(),
        host=host,
        query=query,
        timeout=timeout,
    )
    result = called(response.body)
    if result.ok and (kind := TOOLS[name].result) is not None:
        msgspec.convert(result.data, kind)
    return result


async def wait_for(
    response: Response,
    condition: Callable[[Response], bool],
    timeout: float = 5,
) -> None:
    """Wait until `condition` holds for the records read so far."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition(response):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            lifecycles = [
                {
                    "event": record["event"],
                    **{
                        key: value
                        for key, value in record["data"][
                            record["event"]
                        ].items()
                        if key in {"id", "run_id", "cell_id", "status"}
                    },
                }
                for record in response.records
                if record.get("event") in {"run", "execution"}
            ]
            raise TimeoutError(
                f"gave up waiting after {timeout}s; saw {response.names()}; "
                f"lifecycle events: {lifecycles}"
            )
        response.changed.clear()
        try:
            await asyncio.wait_for(response.changed.wait(), remaining)
        except TimeoutError:
            continue


def until_ready(response: Response) -> bool:
    return "ready" in response.names()
