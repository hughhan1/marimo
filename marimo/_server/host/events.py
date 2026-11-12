# Copyright 2026 Marimo. All rights reserved.
"""The host's events as their protocol messages, and marimo's own events."""

from __future__ import annotations

import msgspec

from marimo._core import protocol
from marimo._core.stream import ExtensionEvent, RoutedEvent
from marimo._messaging.notification import NotificationMessage


class MarimoMessage(msgspec.Struct, frozen=True):
    """A marimo notification carried by the host event stream.

    This type is internal to marimo and may change between releases.
    """

    marimo: NotificationMessage


class MarimoEvent(ExtensionEvent, frozen=True, tag="marimo"):
    """A typed envelope for internal marimo notifications."""

    data: MarimoMessage


def notification_event(
    notebook_id: protocol.NotebookId | None, notification: NotificationMessage
) -> RoutedEvent:
    """Wrap a notification with its notebook and optional run scope."""
    run_id = getattr(notification, "run_id", None)
    return RoutedEvent(
        MarimoEvent(MarimoMessage(notification)),
        notebook_id=notebook_id,
        run_id=protocol.RunId(run_id) if run_id is not None else None,
    )


def _visible_event(
    event: RoutedEvent, *, include_code: bool
) -> protocol.Event | ExtensionEvent:
    """Encode a frame using this connection's source-visibility policy.

    Project a copy at delivery, so the host retains source and revisions for
    snapshots, replay, and staleness checks regardless of the reader.
    """
    message = event.event
    if not include_code and isinstance(message, protocol.CellEvent):
        cell = msgspec.structs.replace(message.data.cell, code=None)
        data = msgspec.structs.replace(message.data, cell=cell)
        message = msgspec.structs.replace(message, data=data)
    return message


def serialize_event(event: RoutedEvent, *, include_code: bool = True) -> str:
    """Encode an event as its protocol message."""
    return msgspec.json.encode(
        _visible_event(event, include_code=include_code)
    ).decode()
