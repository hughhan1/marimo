# Copyright 2026 Marimo. All rights reserved.
"""Call a running marimo server's tools, and follow its sessions, from a test."""

from __future__ import annotations

import json
import queue
import socket
import threading
import urllib.request
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx

from marimo._server.codes import WebSocketCloseReason, WebSocketCodes

if TYPE_CHECKING:
    from typing import Self

PROTOCOL_VERSION = "2026-07-28"


def request_meta() -> dict[str, Any]:
    """The metadata every request must carry."""
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {
            "name": "tests",
            "version": "0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def request_headers(method: str, name: str | None = None) -> dict[str, str]:
    """The headers that mirror a request's method and, for a call, its tool."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def tool_payload(
    name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments or {},
            "_meta": request_meta(),
        },
    }


def tool_request(
    url: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    *,
    endpoint: str = "/api/mcp",
) -> urllib.request.Request:
    """A call to a tool at `endpoint` below the server's `url`."""
    return urllib.request.Request(
        f"{url}{endpoint}",
        method="POST",
        data=json.dumps(tool_payload(name, arguments)).encode(),
        headers={
            "Content-Type": "application/json",
            **request_headers("tools/call", name),
            **(headers or {}),
        },
    )


def tool_result(response: Any) -> dict[str, Any]:
    result: dict[str, Any] = json.load(response)["result"]
    return result


def tool_problem(result: dict[str, Any]) -> dict[str, Any] | None:
    """The problem a failed call's result carries, None for a success."""
    if not result.get("isError"):
        return None
    problem: dict[str, Any] = json.loads(result["content"][0]["text"])
    return problem


class Called(NamedTuple):
    """A tool call's outcome: its result's data, or the problem that failed it."""

    ok: bool
    data: Any = None
    problem: Any = None


def called(body: bytes) -> Called:
    """The outcome of a `tools/call` the server answered with `body`.

    A call refused before it reaches the transport, such as one without the
    server's credentials, fails with the response's own body, and one the
    transport refuses with its JSON-RPC error.
    """
    message = json.loads(body)
    if "jsonrpc" not in message:
        return Called(False, problem=message)
    if "error" in message:
        return Called(False, problem=message["error"])
    result = message["result"]
    problem = tool_problem(result)
    if problem is not None:
        return Called(False, problem=problem)
    return Called(True, data=result["structuredContent"])


def tool_status(result: dict[str, Any]) -> int:
    """The status a result stands for: its problem's, or 200 for a success."""
    problem = tool_problem(result)
    return 200 if problem is None else int(problem["status"])


class SessionClosed(Exception):
    """A session's event stream ended, with the code and reason it gave."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(code, reason)
        self.code = code
        self.reason = reason


def _closed(error: dict[str, Any]) -> SessionClosed:
    if error.get("code") == -32024:  # The draft's Forbidden
        return SessionClosed(
            WebSocketCodes.UNAUTHORIZED, WebSocketCloseReason.UNAUTHORIZED
        )
    data = error.get("data") or {}
    return SessionClosed(
        WebSocketCodes.UNEXPECTED_ERROR,
        str(data.get("reason", error.get("message", ""))),
    )


def stream_headers(name: str) -> dict[str, str]:
    """The headers of a subscription to the event `name`, which streams."""
    return request_headers("events/stream", name)


def stream_payload(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A subscription to the event `name`, pushed on the request's stream."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "events/stream",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": request_meta(),
        },
    }


def stream_frame(method: str, params: dict[str, Any]) -> bytes:
    """One frame of an `events/stream` response: an events notification."""
    message = {
        "jsonrpc": "2.0",
        "method": f"notifications/events/{method}",
        "params": params,
    }
    return f"event: message\ndata: {json.dumps(message)}\n\n".encode()


def session_message(message: dict[str, Any]) -> str | SessionClosed | None:
    """One message of a `session` stream: a frame's text, or how it ended."""
    params = message.get("params", {})
    if message.get("method") == "notifications/events/event":
        return str(params["data"]["frame"])
    if message.get("method") == "notifications/events/terminated":
        return _closed(params.get("error") or {})
    if "error" in message:
        return _closed(message["error"])
    if "result" in message:
        return SessionClosed(WebSocketCodes.NORMAL_CLOSE)
    return None


class SessionEvents:
    """A session on a running server, followed through its `session` event.

    Reads as the session's socket did: `recv` returns one frame's text.
    """

    def __init__(
        self,
        url: str,
        arguments: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._frames: queue.Queue[str | SessionClosed] = queue.Queue()
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, read=None))
        self._response = self._client.send(
            self._client.build_request(
                "POST",
                f"{url}/api/mcp",
                json=stream_payload("session", arguments),
                headers={**stream_headers("session"), **(headers or {})},
            ),
            stream=True,
        )
        if not self._response.headers.get("content-type", "").startswith(
            "text/event-stream"
        ):
            # A refused subscription is answered in JSON, not as a stream.
            self._response.read()
            self.close()
            raise _closed(self._response.json().get("error") or {})
        threading.Thread(target=self._read, daemon=True).start()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def recv(self, timeout: float | None = None) -> str:
        try:
            frame = self._frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no frame arrived in time") from None
        if isinstance(frame, SessionClosed):
            self._frames.put(frame)
            raise frame
        return frame

    def close(self) -> None:
        """End the subscription, as closing the session's socket did."""
        stream = self._response.extensions.get("network_stream")
        if stream is not None:
            # Wakes the reader, which a close alone leaves waiting.
            try:
                stream.get_extra_info("socket").shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._response.close()
        self._client.close()

    def _read(self) -> None:
        closed = SessionClosed(WebSocketCodes.NORMAL_CLOSE)
        try:
            for line in self._response.iter_lines():
                if not line.startswith("data: "):
                    continue
                read = session_message(json.loads(line[len("data: ") :]))
                if isinstance(read, SessionClosed):
                    closed = read
                    return
                if read is not None:
                    self._frames.put(read)
        except (httpx.HTTPError, OSError) as error:
            closed = SessionClosed(1006, str(error))
        finally:
            self._frames.put(closed)


def connect_session(
    url: str,
    session_id: str,
    *,
    file: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> SessionEvents:
    """Follow a session on the server at `url`, as its page does."""
    arguments: dict[str, Any] = {"session_id": session_id}
    if file is not None:
        arguments["file"] = file
    return SessionEvents(url, arguments, headers, timeout)
