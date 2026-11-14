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
import threading
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
    session_state,
    stream_headers,
    stream_payload,
    tool_payload,
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
        running: Future[None] | None,
    ) -> None:
        self._chunks = chunks
        self._buffer = ""
        self._closed: WebSocketDisconnect | None = None
        self._disconnected = disconnected
        self._running = running
        self.closing = WebSocketDisconnect(WebSocketCodes.NORMAL_CLOSE)
        """How the stream ends once its frames are read."""
        self.portal = portal
        """The portal whose loop serves the subscription."""

    def close(self) -> None:
        """End the subscription, as a page closing its socket, and wait."""
        self.portal.call(self._disconnected.set)
        if self._running is not None:
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
                self._closed = self.closing
                raise self._closed
            self._buffer += str(chunk)
        line, self._buffer = self._buffer.split("\n", 1)
        return line.rstrip("\r")


def _as_event(frame: str) -> str:
    """A frame of the state, as the stream would carry it."""
    notification = {
        "method": "notifications/events/event",
        "params": {"data": json.loads(frame)},
    }
    return f"data: {json.dumps(notification)}\n"


@contextmanager
def connect_session(
    client: TestClient,
    url: str,
    headers: dict[str, str] | None = None,
    *,
    resume: str | None = None,
    read_only: list[str] | None = None,
) -> Iterator[SessionEvents]:
    """Subscribe to the session a `/ws` URL named, for the block's duration.

    The URL's path before `/ws` is where the app is mounted. With `resume`,
    the subscription resumes from that cursor without reading the state.
    With `read_only`, the state is read and its cursor appended to it, and
    nothing is followed.
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

    def scope(method_headers: dict[str, str]) -> dict[str, Any]:
        sent = {
            name.lower(): value
            for name, value in {
                **method_headers,
                "content-type": "application/json",
                "host": "testserver",
                **(headers or {}),
            }.items()
        }
        return {
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

    async def read_state() -> bytes:
        body = json.dumps(tool_payload("session_state", arguments)).encode()
        answered: list[bytes] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                answered.append(message.get("body", b""))

        await client.app(
            scope(request_headers("tools/call", "session_state")),
            receive,
            send,
        )
        return b"".join(answered)

    if read_only is not None:
        _, read = session_state(portal.call(read_state))
        assert isinstance(read, str)
        read_only.append(read)
        yield SessionEvents(chunks, portal, portal.call(anyio.Event), None)
        return
    frames, cursor = (
        ([], resume)
        if resume is not None
        else session_state(portal.call(read_state))
    )
    if isinstance(cursor, SessionClosed):
        if not frames:
            raise WebSocketDisconnect(cursor.code, cursor.reason)
        for frame in frames:
            chunks.put(_as_event(frame))
        chunks.put(_END)
        closed = SessionEvents(chunks, portal, portal.call(anyio.Event), None)
        closed.closing = WebSocketDisconnect(cursor.code, cursor.reason)
        yield closed
        return
    stream: queue.Queue[object] = queue.Queue()
    disconnected = portal.call(anyio.Event)

    async def run() -> None:
        requested = False
        body = json.dumps(
            stream_payload("session", arguments, cursor)
        ).encode()

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
                    stream.put(text)

        try:
            await client.app(scope(stream_headers("session")), receive, send)
        finally:
            stream.put(_END)

    running = portal.start_task_soon(run)
    # As a page does, the state is read once following it is confirmed: by
    # the stream's first message, or its refusal.
    opening: list[object] = []
    while not opening or not any(
        chunk is _END or "\n\n" in str(chunk).replace("\r", "")
        for chunk in opening
    ):
        opening.append(stream.get(timeout=_TIMEOUT))
    if "notifications/events/active" in "".join(map(str, opening)):
        for frame in frames:
            chunks.put(_as_event(frame))
    for chunk in opening:
        chunks.put(chunk)

    def forward() -> None:
        while (chunk := stream.get()) is not _END:
            chunks.put(chunk)
        chunks.put(_END)

    threading.Thread(target=forward, daemon=True).start()
    events = SessionEvents(chunks, portal, disconnected, running)
    try:
        yield events
    finally:
        events.close()
