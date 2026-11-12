# Copyright 2026 Marimo. All rights reserved.
"""Analysis's experimental contract and its compatibility with stable events."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from contract import EVENTS, Event, Subscription, examples, payloads
from generate import openapi
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SPEC = openapi(experimental=True)
NOTEBOOK = next(e for e in SPEC["x-events"] if e["name"] == "notebook")


def conform(schema: dict[str, Any], value: Any) -> None:
    Draft202012Validator(
        {**schema, "components": SPEC["components"]},
        format_checker=FormatChecker(),
    ).validate(value)


def validate(name: str, value: Any) -> None:
    conform({"$ref": f"#/components/schemas/{name}"}, value)


@pytest.mark.parametrize(
    ("filename", "schema"),
    [("analysis.json", "Analysis"), ("diagnostic.json", "Error")],
)
def test_examples(filename: str, schema: str) -> None:
    validate(schema, json.loads((FIXTURES / filename).read_text()))


def test_schemas_are_valid() -> None:
    for schema in SPEC["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("message", [{}, {"analysis": {}}])
def test_invalid_analysis_messages_are_rejected(message: Any) -> None:
    with pytest.raises(ValidationError):
        validate("AnalysisMessage", message)


def test_the_notebook_event_carries_analysis() -> None:
    assert "analysis" in payloads(NOTEBOOK, SPEC["components"]["schemas"])


def test_analysis_follows_document_and_matches_revision() -> None:
    document = None
    found = []
    for event in examples(FIXTURES / "notebook-events.sse"):
        conform(NOTEBOOK["payloadSchema"], event)
        if event["event"] == "document":
            document = event["data"]["document"]
        elif event["event"] == "analysis":
            analysis = event["data"]["analysis"]
            assert document is not None
            assert analysis["document_etag"] == document["etag"]
            assert analysis["notebook_id"] == document["notebook_id"]
            assert set(analysis["cells"]) == set(document["order"])
            found.append(analysis)
    assert found == [json.loads((FIXTURES / "analysis.json").read_text())]


async def test_stable_probe_ignores_experimental_payloads() -> None:
    # A stable consumer must not validate a contract it has not selected.
    events = [
        {"event": "analysis", "id": "1", "data": {}},
        {"event": "ready", "id": "1", "data": {}},
    ]

    async def messages() -> AsyncIterator[Any]:
        yield {"method": "notifications/events/active", "params": {}}
        for event in events:
            yield {
                "method": "notifications/events/event",
                "params": {"data": event},
            }

    subscription = Subscription(messages(), 1, payloads(EVENTS["notebook"]))
    assert await subscription.ready() == [Event(**event) for event in events]
