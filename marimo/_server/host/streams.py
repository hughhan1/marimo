# Copyright 2026 Marimo. All rights reserved.
"""The host's two streams, `catalog` and `notebook`, served as MCP events.

A subscription names what it follows in its arguments, and the host sends
it a snapshot or replay that ends in `ready`, then each change, as targeted
occurrences whose data is the protocol's event. A subscription the host
refuses ends with the problem as its error's data.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, get_args

import msgspec
from holon.ext.events import Events
from mcp.types import ErrorData
from starlette.requests import Request

from marimo import _loggers
from marimo._core import protocol
from marimo._core.errors import HostError
from marimo._core.operations import problem, problem_for
from marimo._core.protocol_tools import EVENTS
from marimo._server.api.tools import dedented
from marimo._server.host.events import serialize_event
from marimo._server.host.notebook_api import _context
from marimo._utils.asyncio_utils import cancel_and_wait

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from holon.ext.events import EventType
    from mcp.server.context import ServerRequestContext

    from marimo._core.stream import RoutedEvent
    from marimo._server.host.bridge import SessionBridge

LOGGER = _loggers.marimo_logger()

# The draft leaves -32000 to -32019 to implementations.
_REFUSED = -32001

KINDS = frozenset(get_args(get_args(protocol.ObjectType)[0])) | {"marimo"}
"""The kinds `types` may name."""


class _EventSubscription:
    def __init__(
        self, initial: list[RoutedEvent], queue: asyncio.Queue[RoutedEvent]
    ) -> None:
        self.initial = initial
        self._queue = queue
        self._ready_id = initial[-1].position or 0

    async def next(self) -> RoutedEvent:
        """Wait for a live event not already covered by the initial state."""
        while True:
            event = await self._queue.get()
            if event.position is None or event.position > self._ready_id:
                return event


@contextmanager
def subscribe_events(
    bridge: SessionBridge,
    cursor: str | None,
    types: set[str] | None,
    *,
    notebook_id: protocol.NotebookId | None,
    run_id: str | None = None,
) -> Iterator[_EventSubscription]:
    """The snapshot or replay from `cursor`, then the live events after it."""
    queue: asyncio.Queue[RoutedEvent] = asyncio.Queue()
    # Subscribe and capture state in one event-loop turn to avoid lost updates.
    unsubscribe = bridge.host.subscribe(
        queue.put_nowait,
        notebook_id=notebook_id,
        types=types,
        run_id=run_id,
    )
    try:
        initial = bridge.host.events(
            cursor, notebook_id=notebook_id, types=types, run_id=run_id
        )
        yield _EventSubscription(initial, queue)
    finally:
        unsubscribe()


def _types(asked: tuple[str, ...] | msgspec.UnsetType) -> set[str] | None:
    if asked is msgspec.UNSET or not asked:
        return None
    unknown = set(asked) - KINDS
    if unknown:
        raise _Refused(
            problem(
                "invalid-request",
                f"types has no kind named {', '.join(sorted(unknown))}",
            )
        )
    return set(asked)


def _cursor(cursor: str | msgspec.UnsetType) -> str | None:
    if cursor is msgspec.UNSET:
        return None
    if cursor == "":
        raise _Refused(problem("invalid-request", "cursor is empty"))
    return cursor


class _Refused(Exception):
    def __init__(self, refusal: protocol.Problem) -> None:
        super().__init__(refusal.title)
        self.problem = refusal


def host_events() -> Events:
    """The host's MCP events: `catalog` and `notebook`."""
    served = Events()

    def follow(
        event_type: EventType[Any, Any],
        subscription_id: str,
        bridge: SessionBridge,
        cursor: str | None,
        types: set[str] | None,
        *,
        notebook_id: protocol.NotebookId | None,
        run_id: str | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> Callable[[], Awaitable[None]]:
        def send(event: RoutedEvent) -> None:
            event_type.emit(
                msgspec.json.decode(serialize_event(event)),
                subscription_id=subscription_id,
            )

        async def deliver() -> None:
            try:
                with subscribe_events(
                    bridge,
                    cursor,
                    types,
                    notebook_id=notebook_id,
                    run_id=run_id,
                ) as subscription:
                    for event in subscription.initial:
                        send(event)
                    while True:
                        send(await subscription.next())
            except Exception:
                # A subscription that stopped delivering must not stay open.
                LOGGER.exception("The host failed a %s event", event_type.name)
                refuse(
                    event_type,
                    subscription_id,
                    problem("internal", "The host failed"),
                )
            finally:
                if on_close is not None:
                    on_close()

        delivering = asyncio.create_task(deliver())
        return partial(cancel_and_wait, delivering)

    def refuse(
        event_type: EventType[Any, Any],
        subscription_id: str,
        refusal: protocol.Problem,
    ) -> None:
        event_type.terminate(
            subscription_id,
            ErrorData(
                code=_REFUSED,
                message=refusal.title,
                data={"problem": msgspec.to_builtins(refusal)},
            ),
        )

    async def on_catalog(
        context: ServerRequestContext[Any, Any],
        arguments: dict[str, Any],
        subscription_id: str,
    ) -> Callable[[], Awaitable[None]] | None:
        assert isinstance(context.request, Request)
        host = _context(context.request)
        try:
            asked = msgspec.convert(arguments, protocol.CatalogEventsRequest)
            cursor, types = _cursor(asked.cursor), _types(asked.types)
        except _Refused as refused:
            refuse(catalog, subscription_id, refused.problem)
            return None
        # Refresh on subscribing because the catalog has no directory watcher.
        await host.bridge.refresh()
        return follow(
            catalog,
            subscription_id,
            host.bridge,
            cursor,
            types,
            notebook_id=None,
        )

    async def on_notebook(
        context: ServerRequestContext[Any, Any],
        arguments: dict[str, Any],
        subscription_id: str,
    ) -> Callable[[], Awaitable[None]] | None:
        assert isinstance(context.request, Request)
        host = _context(context.request)
        try:
            asked = msgspec.convert(arguments, protocol.NotebookEventsRequest)
            cursor, types = _cursor(asked.cursor), _types(asked.types)
            notebook = await host.bridge.host.notebook(asked.notebook_id)
            attachment = notebook.attach(
                identity=_given(asked.attachment_identity),
                kind=_given(asked.attachment_kind),
                name=_given(asked.attachment_name),
            )
        except _Refused as refused:
            refuse(notebook_events, subscription_id, refused.problem)
            return None
        except HostError as error:
            refuse(notebook_events, subscription_id, problem_for(error))
            return None
        return follow(
            notebook_events,
            subscription_id,
            host.bridge,
            cursor,
            types,
            notebook_id=notebook.id,
            run_id=_given(asked.run_id),
            on_close=lambda: notebook.detach(attachment.id),
        )

    def declare(
        name: str, on_subscribe: Any
    ) -> EventType[Any, dict[str, Any]]:
        event = EVENTS[name]
        _, definitions = msgspec.json.schema_components([event.arguments])
        (arguments,) = definitions.values()
        return served.event(
            name,
            description=event.description,
            input_schema=dedented(arguments),
            payload_schema=dedented(msgspec.json.schema(event.payload)),
            delivery=("push",),
            on_subscribe=on_subscribe,
        )

    catalog = declare("catalog", on_catalog)
    notebook_events = declare("notebook", on_notebook)
    return served


def _given(value: str | msgspec.UnsetType) -> str | None:
    return None if value is msgspec.UNSET else value
