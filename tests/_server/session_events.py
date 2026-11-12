# Copyright 2026 Marimo. All rights reserved.
"""Follow a session through its `session` MCP event, as tests read a socket.

Starlette's test client waits for a response to finish, and an event stream
does not. This drives the MCP endpoint itself, on one event loop per test
client: MCP Events keep to the loop they are first served on.

`connect_session(client, "/ws?session_id=...")` takes the URL the session's
socket took, and yields an object with the socket's `receive_text`,
`receive_json`, `close` and `portal`. A terminated subscription raises
`WebSocketDisconnect` with the code and reason the socket closed with.
"""

from __future__ import annotations

import json
import queue
import weakref
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
from anyio.from_thread import start_blocking_portal
from starlette.websockets import WebSocketDisconnect

from marimo._server.codes import WebSocketCodes
from tests.mcp_request import (
    SessionClosed,
    request_headers,
    session_message,
    session_payload,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from concurrent.futures import Future

    from anyio.from_thread import BlockingPortal
    from starlette.testclient import TestClient

_TIMEOUT = 30.0
_END = object()


def _portal(client: TestClient) -> BlockingPortal:
    # A client in its `with` block serves everything on its own loop.
    if client.portal is not None:
        return client.portal
    portal: BlockingPortal | None = getattr(client, "_session_events", None)
    if portal is None:
        started = start_blocking_portal()
        portal = started.__enter__()
        weakref.finalize(client, started.__exit__, None, None, None)
        client._session_events = portal  # type: ignore[attr-defined]
    return portal


class SessionEvents:
    """One subscription to the `session` event, read frame by frame."""

    def __init__(
        self,
        chunks: queue.Queue[object],
        portal: BlockingPortal,
        disconnected: anyio.Event,
        running: Future[None],
    ) -> None:
        self._chunks = chunks
        self._buffer = ""
        self._closed: WebSocketDisconnect | None = None
        self._disconnected = disconnected
        self._running = running
        self.portal = portal
        """The portal whose loop serves the subscription."""

    def close(self) -> None:
        """End the subscription, as a page closing its socket, and wait."""
        self.portal.call(self._disconnected.set)
        self._running.result(timeout=_TIMEOUT)

    def receive_text(self) -> str:
        while True:
            if self._closed is not None:
                raise self._closed
            line = self._line()
            if line.startswith("data: "):
                line = line[len("data: ") :]
            elif not line.startswith("{"):
                continue
            # A refused subscription is answered in JSON, not as a stream.
            read = session_message(json.loads(line))
            if isinstance(read, SessionClosed):
                self._closed = WebSocketDisconnect(read.code, read.reason)
            elif read is not None:
                return read

    def receive_json(self) -> Any:
        return json.loads(self.receive_text())

    def _line(self) -> str:
        while "\n" not in self._buffer:
            chunk = self._chunks.get(timeout=_TIMEOUT)
            if chunk is _END:
                if self._buffer:
                    self._chunks.put(_END)
                    line, self._buffer = self._buffer, ""
                    return line
                self._closed = WebSocketDisconnect(WebSocketCodes.NORMAL_CLOSE)
                raise self._closed
            self._buffer += str(chunk)
        line, self._buffer = self._buffer.split("\n", 1)
        return line.rstrip("\r")


@contextmanager
def connect_session(
    client: TestClient,
    url: str,
    headers: dict[str, str] | None = None,
) -> Iterator[SessionEvents]:
    """Subscribe to the session a `/ws` URL named, for the block's duration.

    The URL's path before `/ws` is where the app is mounted.
    """
    parts = urlsplit(url)
    path = f"{parts.path.removesuffix('/ws')}/api/mcp"
    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    arguments: dict[str, Any] = {
        "session_id": query.pop("session_id", None),
        "query_params": query.pop("query_params", ""),
    }
    if arguments["session_id"] is None:
        del arguments["session_id"]
    if "file" in query:
        arguments["file"] = query.pop("file")
    if query.pop("kiosk", None) == "true":
        arguments["kiosk"] = True
    auth = (
        {"access_token": query["access_token"]}
        if "access_token" in query
        else {}
    )
    body = json.dumps(session_payload(arguments)).encode()
    sent = {
        name.lower(): value
        for name, value in {
            **request_headers("events/stream", "session"),
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "host": "testserver",
            **(headers or {}),
        }.items()
    }
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": urlencode(auth).encode(),
        "headers": [(k.encode(), v.encode()) for k, v in sent.items()],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    chunks: queue.Queue[object] = queue.Queue()
    portal = _portal(client)
    disconnected = portal.call(anyio.Event)

    async def run() -> None:
        requested = False

        async def receive() -> dict[str, Any]:
            nonlocal requested
            if not requested:
                requested = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                text = message.get("body", b"").decode()
                if text:
                    chunks.put(text)

        try:
            await client.app(scope, receive, send)
        finally:
            chunks.put(_END)

    events = SessionEvents(
        chunks, portal, disconnected, portal.start_task_soon(run)
    )
    try:
        yield events
    finally:
        events.close()
