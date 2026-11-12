# Copyright 2026 Marimo. All rights reserved.
"""Shared wire examples and regression checks for the conformance probes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from contract import (
    EVENTS,
    REFUSED,
    ROOT,
    SCHEMAS,
    SPEC,
    Event,
    Host,
    Refused,
    document_state,
    examples,
    failed,
    payloads,
    schema_name,
    validate,
)
from jsonschema import Draft202012Validator, ValidationError

FIXTURES = ROOT / "fixtures"
MODELS = {
    "record.json": "HostRecord",
    "cell.json": "Cell",
    "cell-withheld.json": "Cell",
    "edit.json": "EditRequest",
    "stale-edit-request.json": "EditRequest",
    "edit-response.json": "EditResponse",
    "execution-restored.json": "Execution",
    "execute-request.json": "ExecuteRequest",
    "run-cells-request.json": "RunRequest",
    "input-reply-request.json": "InputReplyRequest",
    "input-reply-response.json": "Input",
    "interrupt-runtime-response.json": "Runtime",
}


def answer(*messages: Any, content_type: str = "text/event-stream"):
    """A host that answers with JSON-RPC `messages`, framed as SSE."""
    if content_type == "application/json":
        [message] = messages
        content = json.dumps(message)
    else:
        content = "".join(
            f"event: message\ndata: {json.dumps(message)}\n\n"
            for message in messages
        )
    return lambda _request: httpx.Response(
        200, headers={"Content-Type": content_type}, content=content
    )


def notification(method: str, **params: Any):
    return {
        "jsonrpc": "2.0",
        "method": f"notifications/events/{method}",
        "params": params,
    }


def stream(*events: Any):
    """A host whose subscription carries the protocol `events`."""
    return answer(
        notification("active", cursor="0", truncated=False),
        *(notification("event", name="notebook", data=e) for e in events),
    )


async def snapshot(host, timeout=1):
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1/", transport=httpx.MockTransport(host)
    ) as client:
        return await Host(client, timeout).snapshot(
            "notebook", notebook_id="n1"
        )


def test_schemas_are_valid():
    for schema in SPEC["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


def test_tools_name_their_arguments_results_and_problems():
    problems = {member["$ref"] for member in SCHEMAS["Problem"]["anyOf"]}
    for tool in SPEC["x-tools"]:
        arguments = SCHEMAS[tool["inputSchema"]["$ref"].rsplit("/", 1)[1]]
        assert "idempotency_key" in arguments["required"], tool["name"]
        assert arguments["additionalProperties"] is False, tool["name"]
        if "outputSchema" in tool:
            assert tool["outputSchema"]["$ref"].rsplit("/", 1)[1] in SCHEMAS
        assert {ref["$ref"] for ref in tool["problems"]} <= problems


def test_events_name_their_arguments_and_payloads():
    assert set(EVENTS) == {"catalog", "notebook"}
    for event in SPEC["x-events"]:
        arguments = SCHEMAS[schema_name(event["inputSchema"])]
        assert arguments["additionalProperties"] is False, event["name"]
        assert {"ready", "reset"} <= payloads(event).keys(), event["name"]


@pytest.mark.parametrize(
    ("kind", "listed"), [("conflict", True), ("no-runtime", False)]
)
async def test_a_failure_is_one_of_its_tools_problems(kind, listed):
    value = json.loads((FIXTURES / "problems" / f"{kind}.json").read_text())

    def respond(_request):
        text = json.dumps(value)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": text}],
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1/", transport=httpx.MockTransport(respond)
    ) as client:
        call = Host(client, timeout=1).call(
            "notebook.delete", notebook_id="n1"
        )
        if listed:
            assert failed(await call, kind) == value
        else:
            with pytest.raises(ValidationError):
                await call


@pytest.mark.parametrize(
    "path", sorted(FIXTURES.rglob("*.json")), ids=lambda p: p.name
)
def test_json_examples(path):
    validate(
        "Problem" if path.parent.name == "problems" else MODELS[path.name],
        json.loads(path.read_text()),
    )


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("precondition-failed", "index"),
        ("precondition-failed", "cell"),
        ("precondition-failed", "document"),
        ("decision-required", "decisions"),
    ],
)
def test_problem_variants_require_their_payload(name, field):
    value = json.loads((FIXTURES / "problems" / f"{name}.json").read_text())
    del value[field]
    with pytest.raises(ValidationError):
        validate("Problem", value)


def test_precondition_problem_has_a_fixed_status_and_nullable_cell():
    value = json.loads(
        (FIXTURES / "problems/precondition-failed.json").read_text()
    )
    value["cell"] = None
    validate("Problem", value)
    value["status"] = 409
    with pytest.raises(ValidationError):
        validate("Problem", value)


@pytest.mark.parametrize(
    "path", sorted(FIXTURES.glob("*.sse")), ids=lambda p: p.name
)
def test_stream_examples(path):
    found = examples(path)
    assert found
    for event in found:
        validate("Event", event)


async def test_a_subscription_reads_events_to_ready():
    events = [
        {
            "event": "cell",
            "data": json.loads((FIXTURES / "cell.json").read_text()),
        },
        {"event": "future", "data": {"anything": True}},
        {"event": "ready", "id": "7", "data": {}},
    ]
    events[0]["data"] = {"cell": events[0]["data"]}
    assert await snapshot(stream(*events)) == [
        Event(**event) for event in events
    ]


@pytest.mark.parametrize(
    ("event", "known"),
    [
        ("display.removed", True),
        ("execution.removed", True),
        ("input.removed", False),
        ("document.removed", False),
        ("host.removed", False),
    ],
)
async def test_only_declared_events_are_checked(event, known):
    host = stream(
        {"event": event, "data": {}}, {"event": "ready", "id": "1", "data": {}}
    )
    if known:
        with pytest.raises(ValidationError, match="'id' is a required"):
            await snapshot(host)
    else:
        assert [e.event for e in await snapshot(host)] == [event, "ready"]


@pytest.mark.parametrize("valid", [True, False])
async def test_a_refusal_is_a_problem(valid):
    value = json.loads((FIXTURES / "problems" / "not-found.json").read_text())
    if not valid:
        del value["type"]
    error = {
        "code": REFUSED,
        "message": value["title"],
        "data": {"problem": value},
    }
    host = answer(
        {"jsonrpc": "2.0", "id": 1, "error": error},
        content_type="application/json",
    )
    if valid:
        assert failed(await snapshot(host), "not-found") == value
    else:
        with pytest.raises(ValidationError):
            await snapshot(host)


async def test_a_terminated_subscription_fails():
    host = answer(
        notification("active", cursor="0", truncated=False),
        notification(
            "terminated", error={"code": -32024, "message": "Forbidden"}
        ),
    )
    with pytest.raises(Refused, match="Forbidden"):
        await snapshot(host)


def test_required_nullable_fields_and_future_fields():
    value = json.loads((FIXTURES / "execution-restored.json").read_text())
    validate("Execution", {**value, "future_field": True})
    for field in ("run_id", "runtime_id", "caused_by"):
        missing = {key: item for key, item in value.items() if key != field}
        with pytest.raises(ValidationError):
            validate("Execution", missing)
        with pytest.raises(ValidationError):
            validate("Execution", {**value, field: ""})


def test_cell_source_is_required_but_nullable_only_in_records():
    cell = json.loads((FIXTURES / "cell.json").read_text())
    for code in (None, "", cell["code"]):
        validate("Cell", {**cell, "code": code})
    missing = {key: value for key, value in cell.items() if key != "code"}
    with pytest.raises(ValidationError):
        validate("Cell", missing)
    for value in ({"id": cell["id"]}, {"id": cell["id"], "code": None}):
        with pytest.raises(ValidationError):
            validate("CellInput", value)
    validate("CellInput", {"id": cell["id"], "code": ""})


def notebook_snapshot():
    found = [
        Event(**event) for event in examples(FIXTURES / "notebook-events.sse")
    ]
    return found[: [event.event for event in found].index("ready")]


def test_document_checker_accepts_withheld_source():
    snapshot = notebook_snapshot()
    document, cells = document_state(snapshot)
    for event in snapshot:
        if event.event == "cell":
            event.data["cell"]["code"] = None
    assert document_state(snapshot) == (
        document,
        {key: {**cell, "code": None} for key, cell in cells.items()},
    )


def test_document_checker_rejects_cells_after_the_document():
    snapshot = notebook_snapshot()
    document_state(snapshot)
    with pytest.raises(AssertionError, match="cell arrived after"):
        document_state(
            sorted(snapshot, key=lambda event: event.event != "document")
        )


async def test_stalled_response_headers_have_a_deadline():
    async def stalled(_request):
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await snapshot(stalled, timeout=0.01)


async def test_heartbeats_do_not_extend_a_wait_for_ready():
    async def heartbeats():
        while True:
            await asyncio.sleep(0)
            yield b'data: {"method": "notifications/events/heartbeat", '
            yield b'"params": {}}\n\n'

    def host(_request):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=heartbeats(),
        )

    with pytest.raises(TimeoutError):
        await snapshot(host, timeout=0.01)


def test_code_fingerprints_are_opaque():
    cell = json.loads((FIXTURES / "cell.json").read_text())
    cell["code_fingerprint"] = "custom:v1:opaque"
    validate("Cell", cell)
    cell["code_fingerprint"] = ""
    with pytest.raises(ValidationError):
        validate("Cell", cell)


@pytest.mark.parametrize(
    "case",
    json.loads(
        (ROOT / "conformance/cases/execution-requests.json").read_text()
    ),
    ids=lambda case: f"{case['schema']}: {case['name']}",
)
def test_execution_requests(case):
    # A case is the members a call adds to its notebook and key.
    arguments = {"notebook_id": "n1", "idempotency_key": "k", **case["body"]}
    if case["valid"]:
        validate(case["schema"], arguments)
    else:
        with pytest.raises(ValidationError):
            validate(case["schema"], arguments)
