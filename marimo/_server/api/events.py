# Copyright 2026 Marimo. All rights reserved.
"""The editor's session stream as an MCP event, beside the tools at `/api/mcp`.

A page follows its session by subscribing to the `session` event with the
parameters the session's socket took in its URL. The subscription is the
session's connection: subscribing connects to the session, and ending it
starts the session's time to live. Each subscription starts the page over
with a reset, sent with the session's first frame. Each frame carries the
text the socket sent, since marimo's frames carry numbers JSON cannot, such
as `NaN`. A frame that would have closed the socket terminates the
subscription with its reason.

Events are MCP Events, the Triggers & Events Working Group's draft:
https://github.com/modelcontextprotocol/experimental-ext-triggers-events
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from contextvars import ContextVar
from functools import cache
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from holon.ext.events import Events
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request

from marimo import _loggers
from marimo._core import protocol
from marimo._core.stream import RoutedEvent
from marimo._server.api.deps import AppState
from marimo._server.api.endpoints.ws.ws_connection_validator import (
    ConnectionRejection,
    parse_connection_params,
)
from marimo._server.host.events import serialize_event
from marimo._server.host.lifespan import ensure_host
from marimo._server.host.session import Close, SessionEventStream
from marimo._utils.asyncio_utils import cancel_and_wait

if TYPE_CHECKING:
    from holon.context import RequestContext
    from holon.ext.events import EventType

LOGGER = _loggers.marimo_logger()

request_var: ContextVar[Request] = ContextVar("marimo.events.request")

# The sketch leaves -32000 to -32019 to implementations.
_CLOSED = -32000


class SessionArguments(BaseModel):
    """What a page subscribes to its session with."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    file: str | None = None
    kiosk: bool = False
    query_params: str = ""
    """The page's own query string, which `mo.query_params()` reads."""


class SessionFrame(BaseModel):
    """One frame of the session, as the text its socket sent."""

    frame: str


class _Subscribed(SessionEventStream):
    async def _wait_for_disconnect(self) -> None:
        await asyncio.Future()


def _connection(arguments: SessionArguments) -> Request:
    request = request_var.get()
    query = {
        "session_id": arguments.session_id,
        "query_params": arguments.query_params,
    }
    if arguments.file is not None:
        query["file"] = arguments.file
    if arguments.kiosk:
        query["kiosk"] = "true"
    scope = {**request.scope, "query_string": urlencode(query).encode()}
    return Request(scope, request.receive)


@cache
def events() -> Events:
    """The editor's MCP events: the `session` event alone."""
    served = Events()
    running: dict[str, asyncio.Task[None]] = {}

    def authorize(
        context: RequestContext, arguments: SessionArguments
    ) -> bool:
        del context, arguments
        return "read" in request_var.get().auth.scopes

    async def on_subscribe(
        context: RequestContext,
        arguments: SessionArguments,
        subscription_id: str,
    ) -> None:
        del context
        connection = _connection(arguments)
        app_state = AppState(connection)
        params = parse_connection_params(app_state)
        if isinstance(params, ConnectionRejection):
            terminate(subscription_id, INVALID_PARAMS, params.reason)
            return
        stream = _Subscribed(
            connection,
            notebook=None,
            bridge=(await ensure_host(connection.app)).bridge,
            manager=app_state.session_manager,
            params=params,
            types=None,
            cursor=None,
        )
        running[subscription_id] = asyncio.create_task(
            deliver(stream, subscription_id)
        )

    async def on_unsubscribe(
        context: RequestContext,
        arguments: SessionArguments,
        subscription_id: str,
    ) -> None:
        del context, arguments
        if delivering := running.pop(subscription_id, None):
            await cancel_and_wait(delivering)

    def terminate(subscription_id: str, code: int, reason: str) -> None:
        session.terminate(
            subscription_id,
            ErrorData(code=code, message=reason, data={"reason": reason}),
        )

    def send(text: str, subscription_id: str) -> None:
        session.emit(SessionFrame(frame=text), subscription_id=subscription_id)

    async def deliver(stream: _Subscribed, subscription_id: str) -> None:
        reset = serialize_event(RoutedEvent(protocol.ResetEvent(data={})))
        try:
            async with aclosing(stream.stream()) as frames:
                async for frame in frames:
                    if reset:
                        send(reset, subscription_id)
                        reset = ""
                    if isinstance(frame, Close):
                        terminate(subscription_id, _CLOSED, frame.reason)
                        return
                    send(stream.serialize(frame), subscription_id)
        except Exception:
            # A subscription that stopped delivering must not stay open: the
            # page follows the session again.
            LOGGER.exception("An editor session's stream failed")
            terminate(subscription_id, _CLOSED, "The session's stream failed")

    session: EventType[SessionArguments, SessionFrame] = served.event(
        "session",
        description="Follow an editor session: its notebook and kernel.",
        input_schema=SessionArguments,
        payload_schema=SessionFrame,
        delivery=("push",),
        authorize=authorize,
        on_subscribe=on_subscribe,
        on_unsubscribe=on_unsubscribe,
    )
    return served
