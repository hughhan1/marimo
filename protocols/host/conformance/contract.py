# Copyright 2026 Marimo. All rights reserved.
"""Wire validation, tool calls and SSE probes, independent of host implementations.

A host serves its operations as the MCP tools the spec's `x-tools` lists, at
`/mcp` below its API's URL, and its streams over HTTP. `Host.call` checks a
tool's result against its `outputSchema`, and a failure against its
`problems`.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from generate import openapi
from jsonschema import Draft202012Validator, FormatChecker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import httpx

ROOT = Path(__file__).resolve().parent.parent
SPEC = openapi()
TOOLS = {tool["name"]: tool for tool in SPEC["x-tools"]}
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


def conform(schema: dict[str, Any], value: Any) -> None:
    """Check `value` against a schema that refers to the spec's components."""
    Draft202012Validator(
        {**schema, "components": SPEC["components"]},
        format_checker=FormatChecker(),
    ).validate(value)


def validate(name: str, value: Any) -> None:
    conform({"$ref": f"#/components/schemas/{name}"}, value)


def media_type(response: httpx.Response) -> str:
    return response.headers["content-type"].split(";")[0].strip().lower()


@dataclass(frozen=True)
class Failure:
    """A failed call's problem, which `failed` checks and returns.

    Reading the failure as a result fails the test with the problem, so a
    call that should succeed needs no check of its own.
    """

    problem: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"the call failed: {self.problem}")


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

    async def call(self, name: str, /, **arguments: Any) -> Any:
        """Call the tool `name`, answering its result or its `Failure`.

        `idempotency_key` defaults to a fresh key, and None leaves it out.
        """
        tool = TOOLS[name]
        arguments.setdefault("idempotency_key", uuid4().hex)
        if arguments["idempotency_key"] is None:
            del arguments["idempotency_key"]
        async with asyncio.timeout(self.timeout):
            response = await self.client.post(
                "mcp",
                headers={
                    "Accept": "application/json",
                    "MCP-Protocol-Version": PROTOCOL_VERSION,
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": name,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": name,
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
        assert response.status_code == 200, response.text
        message = response.json()
        assert "result" in message, message
        result = message["result"]
        if result.get("isError"):
            # A problem fails the call, carried as the JSON of one text block.
            [block] = result["content"]
            failure = json.loads(block["text"])
            conform({"anyOf": tool["problems"]}, failure)
            return Failure(failure)
        reply = result.get("structuredContent")
        if "outputSchema" in tool:
            conform(tool["outputSchema"], reply)
        return reply

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
            assert media_type(response) == "text/event-stream"
            assert "access-control-allow-origin" not in response.headers
            yield Watch(frames(response.aiter_lines()), self.timeout)
        finally:
            await context.__aexit__(None, None, None)

    async def snapshot(
        self, path: str = "/events", **kwargs: Any
    ) -> list[Frame]:
        async with self.watch(path, **kwargs) as watch:
            return await watch.ready()


def failed(value: Any, kind: str) -> dict[str, Any]:
    """Check that a call failed with the problem `kind`, and return it."""
    assert isinstance(value, Failure), f"expected {kind}, got {value}"
    assert value.problem["type"] == f"urn:marimo:problem:{kind}", value
    return value.problem


def problem(response: httpx.Response, status: int, kind: str) -> Any:
    """Check that an HTTP response, such as a stream's refusal, is `kind`."""
    assert response.status_code == status, response.text
    assert media_type(response) == "application/problem+json"
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
