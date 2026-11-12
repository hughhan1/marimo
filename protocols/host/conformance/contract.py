# Copyright 2026 Marimo. All rights reserved.
"""Wire validation, tool calls and SSE probes, independent of host implementations.

A host serves its operations as MCP tools at `/mcp` below its API's URL, and
its streams over HTTP. `Host.request` sends a request to an operation's route
in the spec as a call of the operation's tool, whose arguments are the
route's ids, its headers by Python name and the body's members, and answers
with the response the route describes: a success with the operation's status
and the reply, a failure with its problem.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote
from uuid import uuid4

import httpx
from generate import openapi
from jsonschema import Draft202012Validator, FormatChecker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

ROOT = Path(__file__).resolve().parent.parent
SPEC = openapi()
KINDS = (
    "host",
    "project",
    "notebook",
    "document",
    "cell",
    "runtime",
    "attachment",
    "run",
    "execution",
    "console",
    "display",
    "input",
    "variable",
)
SCHEMAS = SPEC["components"]["schemas"]
REMOVAL_EVENTS = {
    name
    for member in SCHEMAS["Event"]["anyOf"]
    for name in SCHEMAS[member["$ref"].rsplit("/", 1)[1]]["properties"][
        "event"
    ]["enum"]
    if name.endswith(".removed")
}


PROTOCOL_VERSION = "2026-07-28"


def _resolve(value: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in value:
        target: Any = SPEC
        for part in value["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        value = target
    return value


@dataclass(frozen=True)
class Route:
    """An operation's route in the spec, which its tool replaces."""

    operation: str
    method: str
    pattern: re.Pattern[str]
    statuses: tuple[int, ...]
    """The statuses the route answers a success with."""
    headers: frozenset[str]
    """The headers the operation reads, by lowercase name."""


def _routes() -> list[Route]:
    found = []
    for path, item in SPEC["paths"].items():
        for method, operation in item.items():
            if not isinstance(operation, dict):
                continue
            statuses = tuple(
                int(status)
                for status, response in operation["responses"].items()
                if status.startswith("2")
                and "text/event-stream"
                not in (_resolve(response).get("content") or {})
            )
            if not statuses:
                # A stream, which stays a request of its own.
                continue
            parameters = [
                _resolve(parameter)
                for parameter in [
                    *item.get("parameters", []),
                    *operation.get("parameters", []),
                ]
            ]
            found.append(
                Route(
                    operation["operationId"],
                    method.upper(),
                    re.compile(
                        re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "/?"
                    ),
                    statuses,
                    frozenset(
                        parameter["name"].lower()
                        for parameter in parameters
                        if parameter["in"] == "header"
                    ),
                )
            )
    return found


ROUTES = _routes()


def validate(name: str, value: Any) -> None:
    schema = {
        "$ref": f"#/components/schemas/{name}",
        "components": SPEC["components"],
    }
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        value
    )


@dataclass
class Frame:
    event: str = "message"
    data: str = ""
    id: str | None = None
    comment: str | None = None

    def message(self) -> dict[str, Any]:
        value = json.loads(self.data)
        if self.event in ("ready", "reset"):
            assert value == {}, f"{self.event} must carry an empty object"
        elif self.event in KINDS:
            validate(f"{self.event.title()}Message", value)
        elif self.event in REMOVAL_EVENTS:
            validate("RemovedMessage", value)
        return value


async def frames(lines: AsyncIterator[str]) -> AsyncIterator[Frame]:
    """Read SSE fields without inheriting ids across snapshot frames."""
    frame = Frame()
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                frame.data = "\n".join(data)
                yield frame
            frame, data = Frame(), []
        elif line.startswith(":"):
            yield Frame(comment=line[1:].removeprefix(" "))
        else:
            field, separator, value = line.partition(":")
            value = value.removeprefix(" ") if separator else ""
            if field == "data":
                data.append(value)
            elif field == "event":
                frame.event = value or "message"
            elif field == "id" and "\0" not in value:
                frame.id = value


class Watch:
    def __init__(self, source: AsyncIterator[Frame], timeout: float):
        self.source = source
        self.timeout = timeout

    async def until(self, predicate: Callable[[Frame], bool]) -> list[Frame]:
        result: list[Frame] = []
        async with asyncio.timeout(self.timeout):
            async for frame in self.source:
                if frame.comment is None and (
                    frame.event in KINDS
                    or frame.event in ("ready", "reset")
                    or frame.event in REMOVAL_EVENTS
                ):
                    frame.message()
                result.append(frame)
                if predicate(frame):
                    return result
        raise AssertionError("stream ended before the expected event")

    async def ready(self) -> list[Frame]:
        return await self.until(lambda frame: frame.event == "ready")


class Host:
    def __init__(self, client: httpx.AsyncClient, timeout: float):
        self.client = client
        self.timeout = timeout

    async def request(
        self, method: str, path: str, *, key: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = {"Idempotency-Key": key if key is not None else uuid4().hex}
        headers.update(kwargs.pop("headers", {}))
        route = next(
            (
                route
                for route in ROUTES
                if route.method == method.upper()
                and route.pattern.fullmatch(path)
            ),
            None,
        )
        async with asyncio.timeout(self.timeout):
            if route is None:
                return await self.client.request(
                    method, path.lstrip("/"), headers=headers, **kwargs
                )
            return await self._call(route, path, headers, kwargs)

    async def _call(
        self,
        route: Route,
        path: str,
        headers: dict[str, str],
        sending: dict[str, Any],
    ) -> httpx.Response:
        """Call the tool that replaces a request to the operation's route."""
        match = route.pattern.fullmatch(path)
        assert match is not None
        arguments: Any = {
            name: unquote(value) for name, value in match.groupdict().items()
        }
        sent: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in route.headers:
                arguments[name.lower().replace("-", "_")] = value
            else:
                sent[name] = value
        body = sending.get("json")
        content = sending.get("content")
        if content:
            try:
                body = json.loads(content)
            except ValueError:
                body = (
                    content if isinstance(content, str) else content.decode()
                )
        if isinstance(body, dict):
            arguments.update(body)
        elif body is not None:
            # Not an object: sent as it is, for the host to refuse.
            arguments = body
        response = await self.client.post(
            "mcp",
            headers={
                **sent,
                "Accept": "application/json",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "tools/call",
                "Mcp-Name": route.operation,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": route.operation,
                    "arguments": arguments,
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": (
                            PROTOCOL_VERSION
                        ),
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
        )
        if response.status_code != 200 or "result" not in response.json():
            return response
        result = response.json()["result"]
        if result.get("isError"):
            # A problem fails the call, carried as the JSON of one text block.
            [block] = result["content"]
            failure = json.loads(block["text"])
            return httpx.Response(
                failure["status"],
                headers={"Content-Type": "application/problem+json"},
                json=failure,
            )
        reply = result["structuredContent"]
        status = route.statuses[0]
        if len(route.statuses) > 1:
            # `runtime.start` answers a runtime it started with a 202 and one
            # it found with a 200; the reply says which by its status.
            status = 202 if reply.get("status") == "starting" else 200
        if status == 204:
            return httpx.Response(204)
        return httpx.Response(status, json=reply)

    @asynccontextmanager
    async def watch(
        self, path: str = "/events", **kwargs: Any
    ) -> AsyncIterator[Watch]:
        # Bound response headers as well as each subsequent wait for events.
        context = self.client.stream("GET", path.lstrip("/"), **kwargs)
        async with asyncio.timeout(self.timeout):
            response = await context.__aenter__()
        try:
            assert response.status_code == 200
            assert (
                response.headers["content-type"].split(";")[0].strip().lower()
                == "text/event-stream"
            )
            assert "access-control-allow-origin" not in response.headers
            yield Watch(frames(response.aiter_lines()), self.timeout)
        finally:
            await context.__aexit__(None, None, None)

    async def snapshot(
        self, path: str = "/events", **kwargs: Any
    ) -> list[Frame]:
        async with self.watch(path, **kwargs) as watch:
            return await watch.ready()


def accepted(response: httpx.Response, status: int, schema: str) -> Any:
    assert response.status_code == status, response.text
    assert (
        response.headers["content-type"].split(";")[0].strip().lower()
        == "application/json"
    )
    value = response.json()
    validate(schema, value)
    return value


def problem(response: httpx.Response, status: int, kind: str) -> Any:
    assert response.status_code == status, response.text
    assert (
        response.headers["content-type"].split(";")[0].strip().lower()
        == "application/problem+json"
    )
    value = response.json()
    validate("Problem", value)
    assert value["status"] == status
    assert value["type"] == f"urn:marimo:problem:{kind}"
    return value


def objects(snapshot: list[Frame], kind: str) -> list[dict[str, Any]]:
    return [frame.message()[kind] for frame in snapshot if frame.event == kind]


def document_state(
    snapshot: list[Frame],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check document ordering using only observed wire records."""
    cells: dict[str, Any] = {}
    document = None
    for frame in snapshot:
        if frame.event == "cell":
            assert document is None, "cell arrived after the document"
            cell = frame.message()["cell"]
            assert cell["id"] not in cells, "duplicate cell in snapshot"
            cells[cell["id"]] = cell
        elif frame.event == "document":
            assert document is None, "duplicate document in snapshot"
            document = frame.message()["document"]
    assert document is not None, "snapshot has no document"
    assert sorted(document["order"]) == sorted(cells)
    for cell in cells.values():
        assert cell["notebook_id"] == document["notebook_id"]
        assert isinstance(cell["code_fingerprint"], str)
        assert cell["code_fingerprint"]
    assert document["version"] >= 1
    for cell in cells.values():
        assert 1 <= cell["version"] <= document["version"]
    return document, cells
