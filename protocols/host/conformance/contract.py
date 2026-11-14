# Copyright 2026 Marimo. All rights reserved.
"""Wire validation, tool calls and event subscriptions, independent of hosts.

A host serves its operations as the MCP tools the spec's `x-tools` lists, and
its streams as the MCP events its `x-events` lists, at `/mcp` below its API's
URL. `Host.call` checks a tool's result against its `outputSchema`, and a
failure against its `problems`. `Host.subscribe` checks each protocol event
against its event's `payloadSchema`, and a refusal against `Problem`.
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
EVENTS = {event["name"]: event for event in SPEC["x-events"]}
SCHEMAS = SPEC["components"]["schemas"]

PROTOCOL_VERSION = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}
# The code of a subscription the host refuses with a problem.
REFUSED = -32001


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


def schema_name(reference: dict[str, Any]) -> str:
    return str(reference["$ref"]).rsplit("/", 1)[1]


def payloads(
    event: dict[str, Any], schemas: dict[str, Any] = SCHEMAS
) -> dict[str, dict[str, Any]]:
    """The member of an event's `payloadSchema` for each name it declares."""
    return {
        name: member
        for member in schemas[schema_name(event["payloadSchema"])]["anyOf"]
        for name in schemas[schema_name(member)]["properties"]["event"]["enum"]
    }


@dataclass(frozen=True)
class Failure:
    """A failed call's problem, which `failed` checks and returns.

    Reading the failure as a result fails the test with the problem, so a
    call that should succeed needs no check of its own.
    """

    problem: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"the call failed: {self.problem}")


class Refused(AssertionError):
    """The host refused a subscription or ended it with a JSON-RPC error."""

    def __init__(self, error: dict[str, Any]):
        super().__init__(f"the subscription was refused: {error}")
        self.error = error
        self.problem: dict[str, Any] | None = None
        if error.get("code") == REFUSED:
            self.problem = error["data"]["problem"]
            validate("Problem", self.problem)
            assert error["message"] == self.problem["title"], error


@dataclass(frozen=True)
class Event:
    """A protocol event: what one MCP event occurrence carries."""

    event: str
    data: dict[str, Any]
    id: str | None = None


async def messages(lines: AsyncIterator[str]) -> AsyncIterator[Any]:
    """The JSON-RPC messages of an SSE-framed MCP response."""
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                yield json.loads("\n".join(data))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


def examples(path: Path) -> list[dict[str, Any]]:
    """The protocol events of a fixture, which writes them as SSE."""
    found = []
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if not line.startswith(":")
        )
        if fields:
            event = {
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
            if "id" in fields:
                event["id"] = fields["id"]
            found.append(event)
    return found


class Subscription:
    """The protocol events of one `events/stream` subscription."""

    def __init__(
        self,
        source: AsyncIterator[Any],
        timeout: float,
        declared: dict[str, dict[str, Any]],
    ):
        self.timeout = timeout
        self._events = self._read(source, declared)

    @staticmethod
    async def _read(
        source: AsyncIterator[Any], declared: dict[str, dict[str, Any]]
    ) -> AsyncIterator[Event]:
        active = False
        async for message in source:
            if "id" in message:
                # The answer to `events/stream` ends the subscription.
                if "error" in message:
                    raise Refused(message["error"])
                return
            method, params = message["method"], message["params"]
            if method == "notifications/events/active":
                active = True
            elif method == "notifications/events/terminated":
                raise Refused(params["error"])
            elif method == "notifications/events/error":
                raise AssertionError(f"the host failed an event: {params}")
            elif method == "notifications/events/event":
                assert active, (
                    "an event arrived before the subscription was active"
                )
                value = params["data"]
                assert isinstance(value.get("event"), str), value
                assert isinstance(value.get("data"), dict), value
                # Clients ignore event names they do not recognize.
                if value["event"] in declared:
                    conform(declared[value["event"]], value)
                yield Event(value["event"], value["data"], value.get("id"))

    async def until(self, predicate: Callable[[Event], bool]) -> list[Event]:
        result: list[Event] = []
        async with asyncio.timeout(self.timeout):
            async for event in self._events:
                result.append(event)
                if predicate(event):
                    return result
        raise AssertionError(
            "the subscription ended before the expected event"
        )

    async def ready(self) -> list[Event]:
        return await self.until(lambda event: event.event == "ready")


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
                    "Accept": "application/json, text/event-stream",
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
                        "_meta": META,
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
    async def subscribe(
        self, name: str, /, **arguments: Any
    ) -> AsyncIterator[Subscription]:
        """Subscribe to the event `name` until the context ends.

        A refusal raises `Refused`.
        """
        context = self.client.stream(
            "POST",
            "mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "events/stream",
                "Mcp-Name": name,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events/stream",
                "params": {
                    "name": name,
                    "arguments": arguments,
                    "_meta": META,
                },
            },
        )
        # Bound the response's headers as well as each wait for events.
        async with asyncio.timeout(self.timeout):
            response = await context.__aenter__()
        try:
            assert "access-control-allow-origin" not in response.headers
            if media_type(response) == "application/json":
                async with asyncio.timeout(self.timeout):
                    message = json.loads(await response.aread())
                assert "error" in message, message
                raise Refused(message["error"])
            assert response.status_code == 200, response.status_code
            assert media_type(response) == "text/event-stream"
            yield Subscription(
                messages(response.aiter_lines()),
                self.timeout,
                payloads(EVENTS[name]),
            )
        finally:
            await context.__aexit__(None, None, None)

    async def snapshot(self, name: str, /, **arguments: Any) -> Any:
        """The events up to `ready`, or the refusal's `Failure`."""
        try:
            async with self.subscribe(name, **arguments) as subscription:
                return await subscription.ready()
        except Refused as refused:
            if refused.problem is None:
                raise
            return Failure(refused.problem)


def failed(value: Any, kind: str) -> dict[str, Any]:
    """Check that a call failed with the problem `kind`, and return it."""
    assert isinstance(value, Failure), f"expected {kind}, got {value}"
    assert value.problem["type"] == f"urn:marimo:problem:{kind}", value
    return value.problem


def problem(response: httpx.Response, status: int, kind: str) -> Any:
    """Check that a response the host refused over HTTP is `kind`."""
    assert response.status_code == status, response.text
    assert media_type(response) == "application/problem+json"
    value = response.json()
    validate("Problem", value)
    assert value["status"] == status
    assert value["type"] == f"urn:marimo:problem:{kind}"
    return value


def objects(events: list[Event], kind: str) -> list[dict[str, Any]]:
    return [event.data[kind] for event in events if event.event == kind]


def document_state(
    snapshot: list[Event],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check document ordering using only observed wire records."""
    cells: dict[str, Any] = {}
    document = None
    for event in snapshot:
        if event.event == "cell":
            assert document is None, "cell arrived after the document"
            cell = event.data["cell"]
            assert cell["id"] not in cells, "duplicate cell in snapshot"
            cells[cell["id"]] = cell
        elif event.event == "document":
            assert document is None, "duplicate document in snapshot"
            document = event.data["document"]
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
