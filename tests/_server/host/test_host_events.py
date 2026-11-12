# Copyright 2026 Marimo. All rights reserved.
"""Events encode as their protocol messages and route by notebook."""

from __future__ import annotations

import json
from typing import Any

import pytest

from marimo._core import protocol
from marimo._core.stream import RoutedEvent, Stream, snapshot_event
from marimo._messaging.notification import CompletedRunNotification
from marimo._server.host.events import (
    MarimoEvent,
    MarimoMessage,
    notification_event,
    serialize_event,
)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            snapshot_event(protocol.Host(operations=("runtime.start",))),
            {
                "event": "host",
                "data": {"host": {"operations": ["runtime.start"]}},
            },
        ),
        (
            RoutedEvent(protocol.ReadyEvent(data={}, id="42")),
            {"event": "ready", "data": {}, "id": "42"},
        ),
        (
            RoutedEvent(protocol.ResetEvent(data={})),
            {"event": "reset", "data": {}},
        ),
        (
            notification_event(
                protocol.NotebookId("n1"),
                CompletedRunNotification(run_id="r1"),
            ),
            {
                "event": "marimo",
                "data": {
                    "marimo": {
                        "op": "completed-run",
                        "run_id": "r1",
                        "outcome": "succeeded",
                        "error": None,
                    }
                },
            },
        ),
    ],
)
def test_events_encode_as_their_protocol_messages(
    event: RoutedEvent, expected: dict[str, Any]
) -> None:
    assert json.loads(serialize_event(event)) == expected


def test_extension_routing_and_control_filters() -> None:
    notebook_id = protocol.NotebookId("n1")
    event = notification_event(
        notebook_id, CompletedRunNotification(run_id="r1")
    )
    assert [event.visible_in(scope) for scope in (None, "n1", "n2")] == [
        False,
        True,
        False,
    ]
    assert event.wanted({"marimo"}, "r1")
    assert not event.wanted({"cell"}, "r1")
    assert not event.wanted({"marimo"}, "r2")

    stream = Stream()
    recorded = stream.append(event)
    assert recorded.event == MarimoEvent(
        data=MarimoMessage(CompletedRunNotification(run_id="r1")),
        id=stream.cursor_at(1),
    )
    assert stream.since(0) == [recorded]

    for wire in (
        protocol.ReadyEvent(data={}, id="1"),
        protocol.ResetEvent(data={}),
    ):
        control = RoutedEvent(wire, notebook_id=notebook_id)
        assert control.wanted({"cell"}, "other-run")
        assert not control.visible_in(notebook_id)
