# Copyright 2026 Marimo. All rights reserved.
"""ASGI test requests to the host's tools and events at its `/mcp`.

Direct ASGI requests let tests read a subscription's events while its
response remains open.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import msgspec

from marimo._core.protocol_tools import TOOLS
from marimo._server.host import HOST_API_PATH
from tests.mcp_request import (
    Called,
    called,
    request_headers,
    stream_headers,
    stream_payload,
    tool_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    from starlette.applications import Starlette
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


def socket_snapshot(websocket: SessionEvents) -> list[dict[str, Any]]:
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
    """A response, and the protocol's events if it carries a subscription."""

    def __init__(self) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self.records: list[dict[str, Any]] = []
        """The events received, each `{event, id?, data}`."""
        self.ended: dict[str, Any] | None = None
        """The error the host ended the subscription with."""
        self.body = b""
        self.changed = asyncio.Event()
        self._buffer = ""

    def feed(self, chunk: bytes) -> None:
        self.body += chunk
        self._buffer = (self._buffer + chunk.decode()).replace("\r\n", "\n")
        while "\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n\n", 1)
            message = _message(raw)
            if message.get("method") == "notifications/events/event":
                self.records.append(message["params"]["data"])
            elif message.get("method") == "notifications/events/terminated":
                self.ended = message["params"]["error"]
        self.changed.set()

    def json(self) -> Any:
        return json.loads(self.body)

    def names(self) -> list[str]:
        return [r["event"] for r in self.records]


def _message(record: str) -> dict[str, Any]:
    """The JSON-RPC message a server-sent record carries, if any."""
    for line in record.split("\n"):
        if line.startswith("data:"):
            message: dict[str, Any] = json.loads(line.removeprefix("data:"))
            return message
    return {}


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


def subscribe(
    app: Starlette,
    name: str,
    /,
    *,
    headers: dict[str, str] = AUTH,
    host: str = AUTHORITY,
    client: tuple[str, int] = LOOPBACK,
    **arguments: Any,
) -> tuple[Response, asyncio.Task[None], Callable[[], None]]:
    """Subscribe to the host's event `name`, as a client does.

    Returns what `request` does; the response's records are the events.
    """
    return request(
        app,
        "POST",
        "/mcp",
        headers={**stream_headers(name), **headers},
        body=json.dumps(stream_payload(name, arguments)).encode(),
        host=host,
        client=client,
    )


async def refused(app: Starlette, name: str, /, **kwargs: Any) -> Any:
    """What refuses a subscription before it starts.

    The host's refusal is its problem, which the transport's JSON-RPC error
    carries as data. A subscription refused before the transport, such as
    one without the host's token, fails with the response's own problem,
    and one the transport refuses with its error alone.
    """
    response, task, _ = subscribe(app, name, **kwargs)
    await asyncio.wait_for(task, 5)
    error = called(response.body).problem
    problem = (
        (error.get("data") or {}).get("problem") if "code" in error else None
    )
    if problem is None:
        return error
    assert (error["code"], error["message"]) == (-32001, problem["title"])
    return problem


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
