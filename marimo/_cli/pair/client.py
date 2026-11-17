# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import http.client
import json
import queue
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import msgspec

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable, Iterator, Mapping
    from http.client import HTTPResponse
    from pathlib import Path

    from marimo._core import protocol


class PairError(Exception):
    """A failure that the CLI reports on stderr."""


class PairInputError(PairError):
    """Invalid local input that prevents a server operation."""


class PairConnectionError(PairError):
    """A transport failure while connecting to the notebook server."""


class UnreadableReplyError(PairError):
    """The server's reply could not be read, so its outcome is unknown."""


class NoRuntimeError(PairError):
    """The notebook has no running kernel."""


class RunConflictError(PairError):
    """The host refused the run as a conflict."""


class NoNotebookError(PairError):
    """No notebook was found at the requested server URL."""

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class AmbiguousNotebookError(PairError):
    """Discovery found several notebooks and requires an explicit selection."""

    def __init__(
        self, message: str, *, url: str, candidates: tuple[str, ...]
    ) -> None:
        super().__init__(message)
        self.url = url
        self.candidates = candidates


class StaleNotebookError(PairError):
    """The server rejected the Notebook ID as unknown."""


class NotebookUnsupportedError(PairError):
    """The server discovery does not expose host notebooks."""


@dataclass(frozen=True)
class SSEEvent:
    """A parsed server-sent event with an optional stream resume cursor."""

    name: str
    data: str
    id: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """Captured output and final outcome of a notebook execution."""

    success: bool
    output: dict[str, str] | None
    stdout: str
    stderr: str
    outcome: str = "succeeded"


def load_token(
    token_file: Path | None, environ: Mapping[str, str]
) -> str | None:
    if token_file is None:
        return environ.get("MARIMO_TOKEN") or None

    try:
        token = token_file.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as error:
        raise PairInputError("Could not read the token file.") from error
    if not token:
        raise PairInputError("The token file is empty.")
    return token


def display_url(url: str) -> str:
    """Return a URL without credentials, query values, or a fragment."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid URL>"
    return parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""
    ).geturl()


def _mcp_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise PairInputError("The server URL is invalid.") from error
    return parsed._replace(
        path=f"{parsed.path.rstrip('/')}/mcp", fragment=""
    ).geturl()


def iter_sse(lines: Iterable[bytes]) -> Iterator[SSEEvent]:
    name = "message"
    data: list[str] = []
    cursor: str | None = None

    for raw_line in lines:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield SSEEvent(name=name, data="\n".join(data), id=cursor)
            name = "message"
            data = []
            continue
        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            name = value
        elif field == "id" and "\0" not in value:
            cursor = value
        elif field == "data":
            data.append(value)

    if data:
        yield SSEEvent(name=name, data="\n".join(data), id=cursor)


def pump_events(
    events: Iterable[SSEEvent],
    incoming: queue.Queue[SSEEvent | Exception | None],
    stopped: threading.Event,
) -> None:
    """Read events on a worker thread with bounded, cancellable delivery."""

    def put(value: SSEEvent | Exception | None) -> None:
        while not stopped.is_set():
            try:
                incoming.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    try:
        for event in events:
            if stopped.is_set():
                return
            put(event)
    except (
        PairError,
        OSError,
        http.client.HTTPException,
        ValueError,
        AttributeError,
    ) as error:
        put(error)
    finally:
        put(None)


def open_response(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    read_timeout: float | None = None,
) -> EventStream:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise PairInputError("The server URL is invalid.") from error
    if parsed.scheme not in ("http", "https") or parsed.hostname is None:
        raise PairInputError("The server URL must use http or https.")
    if port is not None and not 1 <= port <= 65535:
        raise PairInputError("The server URL is invalid.")

    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    try:
        connection = connection_type(
            parsed.hostname,
            port,
            timeout=5.0,
        )
        connection.connect()
        assert connection.sock is not None
        response_socket = connection.sock
        response_socket.settimeout(read_timeout)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
    except (ValueError, http.client.InvalidURL) as error:
        connection.close()
        raise PairInputError("The server URL is invalid.") from error
    except (OSError, http.client.HTTPException) as error:
        connection.close()
        raise PairConnectionError(
            "Could not connect to the server."
        ) from error

    try:
        _raise_for_status(response)
    except PairError:
        connection.close()
        raise
    return EventStream(response, response_socket)


def _response_problem(response: HTTPResponse) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(response.read())
    except (OSError, UnicodeError, ValueError, http.client.HTTPException):
        return None, None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        problem = payload.get("type")
        return (
            detail if isinstance(detail, str) else None,
            problem if isinstance(problem, str) else None,
        )
    return None, None


def _raise_for_status(response: HTTPResponse) -> None:
    if 200 <= response.status < 300:
        return
    detail, problem = _response_problem(response)
    response.close()
    raise _failure(response.status, detail, problem)


def _failure(status: object, detail: str | None, problem: object) -> PairError:
    """The error a server's refusal describes."""
    if status in (401, 403):
        return PairError("Authentication failed.")
    if status == 404:
        return StaleNotebookError(detail or "Unknown notebook.")
    if (
        problem == "urn:marimo:problem:no-runtime"
        or detail == "The notebook has no running session."
    ):
        return NoRuntimeError(detail or "The notebook has no running kernel.")
    if problem == "urn:marimo:problem:conflict":
        return RunConflictError(detail or "The host refused the run.")
    if detail:
        return PairError(detail)
    return PairError(
        f"Server returned {status}." if status else "The call failed."
    )


def execute(
    *, url: str, notebook_id: str, token: str | None, code: str
) -> ExecutionResult:
    """Run `code` against a notebook's kernel and return its outcome."""
    unknown = "The execution response ended before completion was confirmed."
    try:
        result = call_tool(
            url=url,
            token=token,
            name="kernel_execute",
            arguments={"code": code, "notebook_id": notebook_id},
        )
    except (PairConnectionError, UnreadableReplyError) as error:
        # The code may have run, so the caller must inspect rather than retry.
        raise PairError(unknown) from error
    if not isinstance(result, dict):
        raise PairError(unknown)
    success = bool(result.get("success"))
    data = result.get("output")
    return ExecutionResult(
        success=success,
        outcome="succeeded" if success else "failed",
        output=None
        if not data
        else {
            "mimetype": str(result.get("output_mimetype") or "text/plain"),
            "data": str(data),
        },
        stdout="".join(result.get("stdout") or []),
        stderr="".join(result.get("stderr") or []),
    )


@dataclass(frozen=True)
class EventStream:
    """An HTTP response whose socket can be closed to unblock its reader."""

    response: HTTPResponse
    socket: socket.socket

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.response)

    def close(self) -> None:
        # Shutdown interrupts a blocked reader before closing its file handle.
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.response.close()
        finally:
            self.socket.close()


def open_events(
    *,
    url: str,
    name: str,
    arguments: dict[str, object],
    token: str | None,
    read_timeout: float | None = 45,
) -> EventStream:
    """Subscribe to one of the host's events at its `/mcp`.

    Read the subscription's events with `iter_events`. A host that refuses
    the subscription answers at once, which raises here.
    """
    stream = open_response(
        method="POST",
        url=_mcp_url(url),
        headers=_mcp_headers("events/stream", name, token),
        body=_mcp_body("events/stream", name, arguments),
        read_timeout=read_timeout,
    )
    if stream.response.getheader("Content-Type", "").startswith(
        "application/json"
    ):
        try:
            payload = json.load(stream.response)
        except (OSError, UnicodeError, ValueError, http.client.HTTPException):
            payload = None
        finally:
            stream.close()
        error = payload.get("error") if isinstance(payload, dict) else None
        raise _refusal(error if isinstance(error, dict) else {})
    return stream


def iter_events(
    lines: Iterable[bytes],
) -> Iterator[SSEEvent]:
    """The host's events in an `events/stream` response, as named events."""
    for frame in iter_sse(lines):
        message = json.loads(frame.data)
        method = message.get("method")
        params = message.get("params") or {}
        if method == "notifications/events/event":
            event = params["data"]
            yield SSEEvent(
                name=event["event"],
                data=json.dumps(event["data"]),
                id=event.get("id"),
            )
        elif method == "notifications/events/terminated":
            raise _refusal(params.get("error") or {})
        elif "error" in message:
            raise _refusal(message["error"])


def _refusal(error: dict[str, object]) -> PairError:
    """The error a refused or ended subscription describes."""
    data = error.get("data")
    problem = data.get("problem") if isinstance(data, dict) else None
    if not isinstance(problem, dict):
        message = error.get("message")
        return PairError(
            str(message) if message else "The host ended the stream."
        )
    detail = str(problem.get("detail") or problem.get("title") or "")
    if problem.get("type") == "urn:marimo:problem:not-found":
        return StaleNotebookError(detail or "Unknown notebook.")
    if problem.get("status") in (401, 403):
        return PairError("Authentication failed.")
    return PairError(detail or "The host ended the stream.")


@dataclass(frozen=True)
class HostEndpoint:
    """Server address and bearer token from a validated live host record."""

    url: str
    token: str | None = field(repr=False)


def find_host(url: str, *, root: Path | None = None) -> HostEndpoint | None:
    from marimo._server.host.record import discover_hosts

    def normalized(value: str) -> tuple[str, str | None, int, str]:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname is None:
            raise PairInputError("The server URL must use http or https.")
        host = parsed.hostname.lower()
        if host == "localhost":
            host = "127.0.0.1"
        return (
            parsed.scheme,
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            parsed.path.rstrip("/"),
        )

    try:
        selected = normalized(url)
    except ValueError as error:
        raise PairInputError("The server URL is invalid.") from error
    for record in discover_hosts(root=root):
        if normalized(record.url) == selected:
            return host_endpoint(record)
    return None


def host_endpoint(record: protocol.HostRecord) -> HostEndpoint:
    return HostEndpoint(
        record.url, None if record.token is msgspec.UNSET else record.token
    )


def resolve_host(url: str, *, root: Path | None = None) -> HostEndpoint:
    host = find_host(url, root=root)
    if host is None:
        raise PairError(
            "No live host record matches this URL. Start a local edit server, or restart the existing server."
        )
    return host


_PROTOCOL_VERSION = "2026-07-28"


def _mcp_headers(method: str, name: str, token: str | None) -> dict[str, str]:
    """The headers of an MCP request, which mirror its method and name."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Mcp-Name": name,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _mcp_body(method: str, name: str, arguments: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                "name": name,
                "arguments": arguments,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
    ).encode()


def call_tool(
    *,
    url: str,
    token: str | None,
    name: str,
    arguments: dict[str, object] | None = None,
) -> object:
    """Call one of the editor's tools and return its structured result."""
    request_url = _mcp_url(url)
    response = open_response(
        method="POST",
        url=request_url,
        headers=_mcp_headers("tools/call", name, token),
        body=_mcp_body("tools/call", name, arguments or {}),
    )
    unexpected = f"Unexpected response from {display_url(request_url)}."
    try:
        try:
            # A long call answers as a stream: pings, then the result.
            payload = (
                next(
                    (
                        json.loads(event.data)
                        for event in iter_sse(response)
                        if event.name == "message"
                    ),
                    None,
                )
                if response.response.getheader("Content-Type", "").startswith(
                    "text/event-stream"
                )
                else json.load(response.response)
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            http.client.HTTPException,
        ) as error:
            raise UnreadableReplyError(unexpected) from error
    finally:
        response.close()
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise UnreadableReplyError(unexpected)
    if result.get("isError"):
        raise _tool_error(result)
    return result.get("structuredContent")


def _tool_error(result: dict[str, object]) -> PairError:
    """The error a failed tool call's problem describes."""
    content = result.get("content")
    text = (
        content[0].get("text")
        if isinstance(content, list)
        and content
        and isinstance(content[0], dict)
        else None
    )
    try:
        problem = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        problem = None
    if not isinstance(problem, dict):
        return PairError(text if isinstance(text, str) else "The call failed.")
    detail = problem.get("detail") or problem.get("title")
    return _failure(
        problem.get("status"),
        str(detail) if detail else None,
        problem.get("type"),
    )


def list_sessions(
    *, url: str, token: str | None
) -> dict[str, dict[str, str | None]]:
    payload = call_tool(url=url, token=token, name="sessions")
    message = f"Unexpected response from {display_url(_mcp_url(url))}."
    if not isinstance(payload, dict):
        raise PairError(message)

    sessions: dict[str, dict[str, str | None]] = {}
    for session_id, session in payload.items():
        if not isinstance(session_id, str) or not isinstance(session, dict):
            raise PairError(message)
        if "filename" not in session or "path" not in session:
            raise PairError(message)
        if "session_id" not in session:
            raise NotebookUnsupportedError
        stable_session_id = session["session_id"]
        filename = session["filename"]
        path = session["path"]
        if (
            not isinstance(stable_session_id, str)
            or (filename is not None and not isinstance(filename, str))
            or (path is not None and not isinstance(path, str))
        ):
            raise PairError(message)
        notebook_id = session.get("notebook_id")
        if notebook_id is not None and not isinstance(notebook_id, str):
            raise PairError(message)
        sessions[session_id] = {
            "session_id": stable_session_id,
            "filename": filename,
            "path": path,
            "notebook_id": notebook_id,
        }
    return sessions
