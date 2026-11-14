# Copyright 2026 Marimo. All rights reserved.
"""The editor's session stream as MCP events at `/api/mcp`."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from contextvars import ContextVar
from functools import partial
from typing import TYPE_CHECKING, Any

from holon.ext.events import Events
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import BaseModel, ConfigDict

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
    from collections.abc import Awaitable, Callable

    from holon.context import RequestContext
    from holon.ext.events import EventType
    from starlette.requests import Request

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
    frame: str


def events() -> Events:
    """The editor's MCP events: the `session` event alone."""
    served = Events()

    def authorize(context: RequestContext, arguments: dict[str, Any]) -> bool:
        del context, arguments
        return "read" in request_var.get().auth.scopes

    async def on_subscribe(
        context: RequestContext,
        arguments: dict[str, Any],
        subscription_id: str,
    ) -> Callable[[], Awaitable[None]] | None:
        del context
        asked = SessionArguments.model_validate(arguments)
        connection = request_var.get()
        app_state = AppState(connection)
        params = parse_connection_params(
            app_state,
            session_id=asked.session_id,
            file=asked.file,
            kiosk=asked.kiosk,
            query_params=asked.query_params,
        )
        if isinstance(params, ConnectionRejection):
            terminate(subscription_id, INVALID_PARAMS, params.reason)
            return None
        stream = SessionEventStream(
            connection,
            bridge=(await ensure_host(connection.app)).bridge,
            manager=app_state.session_manager,
            params=params,
        )
        delivering = asyncio.create_task(deliver(stream, subscription_id))
        return partial(cancel_and_wait, delivering)

    def terminate(subscription_id: str, code: int, reason: str) -> None:
        session.terminate(
            subscription_id,
            ErrorData(code=code, message=reason, data={"reason": reason}),
        )

    def send(text: str, subscription_id: str) -> None:
        session.emit(SessionFrame(frame=text), subscription_id=subscription_id)

    async def deliver(
        stream: SessionEventStream, subscription_id: str
    ) -> None:
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

    session: EventType[dict[str, Any], SessionFrame] = served.event(
        "session",
        description="Follow an editor session: its notebook and kernel.",
        input_schema=SessionArguments.model_json_schema(),
        payload_schema=SessionFrame.model_json_schema(),
        delivery=("push",),
        authorize=authorize,
        on_subscribe=on_subscribe,
    )
    return served
