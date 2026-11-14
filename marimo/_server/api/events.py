# Copyright 2026 Marimo. All rights reserved.
"""The editor's session as state plus changes at `/api/mcp`.

A page reads its session's state with `session_state`, which carries the
`session` event's cursor, and follows the event from that cursor. The
event carries every change to the page's notebook from the host's event
log, and what the session sends the page alone.
"""

from __future__ import annotations

import asyncio
import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, TypeAlias

import msgspec
from holon.ext.events import Events
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import BaseModel, ConfigDict

from marimo._core import protocol
from marimo._core.stream import RoutedEvent
from marimo._messaging.notification import BannerNotification
from marimo._messaging.serde import deserialize_kernel_message
from marimo._server.api.deps import AppState
from marimo._server.api.endpoints.ws.notification_filter import (
    should_filter_operation,
)
from marimo._server.api.endpoints.ws.ws_connection_validator import (
    ConnectionParams,
    ConnectionRejection,
    parse_connection_params,
)
from marimo._server.api.endpoints.ws.ws_kernel_ready import build_kernel_ready
from marimo._server.codes import WebSocketCloseReason
from marimo._server.host.events import (
    MarimoEvent,
    notification_event,
    serialize_event,
)
from marimo._server.host.lifespan import ensure_host
from marimo._server.host.session import Tab
from marimo._session.model import SessionMode
from marimo._utils.asyncio_utils import cancel_and_wait

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from holon.context import RequestContext
    from holon.ext.events import Event, EventType
    from starlette.requests import Request

    from marimo._messaging.notification import NotificationMessage
    from marimo._server.host.bridge import SessionBridge
    from marimo._server.session_manager import SessionManager
    from marimo._session import Session

request_var: ContextVar[Request] = ContextVar("marimo.events.request")

# The sketch leaves -32000 to -32019 to implementations.
_CLOSED = -32000

SessionEvent: TypeAlias = protocol.Event | MarimoEvent
"""One change to a page's session: a host event, or a marimo notification."""

_RESET: dict[str, Any] = {"event": "reset", "data": {}}
_RESUMED = BannerNotification(
    title="Reconnected",
    description="You have reconnected to an existing session.",
    action="restart",
)


class SessionArguments(BaseModel):
    """What a page subscribes to its session with."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    file: str | None = None
    kiosk: bool = False
    query_params: str = ""
    """The page's own query string, which `mo.query_params()` reads."""


class SessionRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The session a page reads, named as it subscribes to it."""

    session_id: str
    file: str | None = None
    kiosk: bool = False
    query_params: str = ""


class SessionState(msgspec.Struct, frozen=True):
    """A page's session as of `cursor`, the `session` event's position.

    `events` are `session` payloads that build the page: a `reset`, the
    notebook's host events ending in `ready`, then the notifications that
    start the editor. They are empty when the page has no session yet:
    subscribing starts one and sends its state. `closed` is why the page
    cannot follow a session at all.
    """

    cursor: str
    events: list[dict[str, Any]]
    closed: str | None = None


@dataclass(frozen=True)
class _Change:
    """A `session` payload as kept, with where it goes."""

    event: dict[str, Any]
    notebook: str | None = None
    """The notebook it changes, for every page that follows it."""
    to: str | None = None
    """The page it is for alone, by session id."""
    op: str | None = None
    """A marimo notification's name, which viewing pages filter by."""


def _wire(event: RoutedEvent, *, include_code: bool = True) -> dict[str, Any]:
    decoded: dict[str, Any] = msgspec.json.decode(
        serialize_event(event, include_code=include_code)
    )
    return decoded


def _params(
    request: Request, arguments: SessionArguments | SessionRequest
) -> ConnectionParams | str:
    params = parse_connection_params(
        AppState(request),
        session_id=arguments.session_id,
        file=arguments.file,
        kiosk=arguments.kiosk,
        query_params=arguments.query_params,
    )
    return params.reason if isinstance(params, ConnectionRejection) else params


def _place(
    manager: SessionManager, params: ConnectionParams
) -> tuple[Session, bool] | None:
    """The session a page is in, or would view, and whether it views it."""
    session = manager.get_session(params.session_id)
    if session is None and (params.kiosk or manager.mode is SessionMode.EDIT):
        session = manager.get_session_by_file_key(params.file_key)
    if session is None:
        return None
    main = session.room.main_consumer
    viewer = params.kiosk or (
        main is not None and main.consumer_id != params.session_id
    )
    return session, viewer


def _handshake(
    manager: SessionManager,
    params: ConnectionParams,
    session: Session,
    *,
    viewer: bool,
) -> list[NotificationMessage]:
    """What starts an editor on a session that already runs."""
    handshake: list[NotificationMessage] = [
        build_kernel_ready(
            session=session,
            # The page that edits a notebook no page instantiated does so.
            resumed=viewer or session.instantiated,
            ui_values=session.session_view.ui_values,
            kiosk=viewer,
        )
    ]
    if not viewer and manager.get_session(params.session_id) is None:
        # The page takes over a session another page left.
        handshake.append(_RESUMED)
    handshake.extend(session.session_view.snapshot_notifications())
    return handshake


class SessionEvents:
    """The `session` event and the state a page follows it from."""

    def __init__(self) -> None:
        self.events = Events()
        self._fed: weakref.WeakSet[SessionBridge] = weakref.WeakSet()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Pages that read no session, whose subscription sends them its state.
        self._unread: set[str] = set()
        self.session: EventType[SessionArguments, _Change] = self.events.event(
            "session",
            description="Follow an editor session: its notebook and kernel.",
            input_schema=SessionArguments,
            payload_schema=msgspec.json.schema(SessionEvent),
            authorize=self._authorize,
            match=self._match,
            transform=self._transform,
            on_subscribe=self._on_subscribe,
        )

    async def state(
        self, request: Request, arguments: SessionRequest
    ) -> SessionState:
        """The page's session, as of the `session` event's cursor."""
        params = _params(request, arguments)
        if isinstance(params, str):
            return SessionState(self.session.cursor(), [], params)
        bridge = await self._feed(request)
        manager = AppState(request).session_manager
        if params.kiosk and manager.mode is not SessionMode.EDIT:
            return SessionState(
                self.session.cursor(),
                [],
                WebSocketCloseReason.KIOSK_NOT_ALLOWED,
            )
        place = _place(manager, params)
        if place is None:
            # Subscribing starts or joins the session, and sends its state.
            self._unread.add(params.session_id)
            return SessionState(self.session.cursor(), [])
        self._unread.discard(params.session_id)
        session, viewer = place
        events = self._snapshot(
            bridge,
            manager,
            session,
            _handshake(manager, params, session, viewer=viewer),
            viewer=viewer,
        )
        return SessionState(self.session.cursor(), events)

    def _snapshot(
        self,
        bridge: SessionBridge,
        manager: SessionManager,
        session: Session,
        handshake: list[NotificationMessage],
        *,
        viewer: bool,
    ) -> list[dict[str, Any]]:
        """A session's state, read without yielding to the event loop."""
        notebook = bridge.notebook_for_session(session)
        if notebook is None:
            return [_RESET]
        include_code = manager.should_send_code_to_frontend()
        return [
            _RESET,
            *(
                _wire(event, include_code=include_code)
                for event in bridge.host.events(None, notebook_id=notebook.id)
                if not isinstance(event.event, MarimoEvent)
            ),
            *(
                _wire(notification_event(notebook.id, message))
                for message in handshake
                if not should_filter_operation(message.name, is_kiosk=viewer)
            ),
        ]

    async def _feed(self, request: Request) -> SessionBridge:
        """The host's bridge, whose event log feeds the `session` event."""
        bridge = (await ensure_host(request.app)).bridge
        self._loop = self._loop or asyncio.get_running_loop()
        if bridge not in self._fed:
            self._fed.add(bridge)
            include_code = AppState(
                request
            ).session_manager.should_send_code_to_frontend()

            def changed(event: RoutedEvent) -> None:
                op = (
                    str(event.event.data.marimo.name)
                    if isinstance(event.event, MarimoEvent)
                    else None
                )
                self._on_loop(
                    self.session.emit,
                    _Change(
                        _wire(event, include_code=include_code),
                        notebook=event.notebook_id,
                        op=op,
                    ),
                )

            bridge.host.subscribe_notebooks(changed)
        return bridge

    def _authorize(
        self, context: RequestContext, arguments: SessionArguments
    ) -> bool:
        del context, arguments
        return "read" in request_var.get().auth.scopes

    def _match(
        self,
        context: RequestContext,
        event: Event[_Change],
        arguments: SessionArguments,
    ) -> bool:
        del context
        change = event.data
        if change.notebook is None:
            return change.to == arguments.session_id
        request = request_var.get()
        session = AppState(request).session_manager.get_session(
            arguments.session_id  # type: ignore[arg-type]
        )
        bridge: SessionBridge = request.app.state.host_context.bridge
        if session is None or session is not bridge.session_for_notebook(
            protocol.NotebookId(change.notebook)
        ):
            return False
        if change.op is None:
            return True
        main = session.room.main_consumer
        viewer = arguments.kiosk or (
            main is not None and main.consumer_id != arguments.session_id
        )
        return not should_filter_operation(change.op, is_kiosk=viewer)

    def _transform(
        self,
        context: RequestContext,
        event: Event[_Change],
        arguments: SessionArguments,
    ) -> object:
        del context, arguments
        return event.data.event

    async def _on_subscribe(
        self,
        context: RequestContext,
        arguments: SessionArguments,
        subscription_id: str,
    ) -> Callable[[], Awaitable[None]] | None:
        """Join the page to its session while it follows it.

        Joining that starts or resumes the session sends the page its new
        state, since what it read before was of no session or another one.
        """
        del context
        request = request_var.get()
        params = _params(request, arguments)
        if isinstance(params, str):
            self._close(subscription_id, params, INVALID_PARAMS)
            return None
        bridge = await self._feed(request)
        manager = AppState(request).session_manager
        to_page = partial(self._to_page, arguments.session_id)
        tab = Tab(
            bridge=bridge,
            manager=manager,
            params=params,
            sent=lambda sent: to_page([deserialize_kernel_message(sent)]),
            closed=partial(self._close, subscription_id),
        )

        async def join(
            before: tuple[Session, bool] | None, refused: int
        ) -> None:
            wrote, reason = await tab.connect(request)
            if reason is not None:
                to_page(wrote)
                self._close(subscription_id, reason, refused)
                return
            place = _place(manager, params)
            unread = params.session_id in self._unread
            self._unread.discard(params.session_id)
            if place is not None and (place != before or unread):
                session, viewer = place
                if not any(each.name == "kernel-ready" for each in wrote):
                    wrote = _handshake(manager, params, session, viewer=viewer)
                for event in self._snapshot(
                    bridge, manager, session, wrote, viewer=viewer
                ):
                    self._on_loop(
                        self.session.emit, _Change(event, to=params.session_id)
                    )
            tab._check_status_update()

        before = _place(manager, params)
        if before is not None:
            # Joining a running session is quick, so the page is in it by
            # the time its subscription is confirmed.
            await join(before, INVALID_PARAMS)
            return tab.leave
        # Starting one takes a while, and the page hears its progress.
        joining = asyncio.create_task(join(before, _CLOSED))

        async def leave() -> None:
            await cancel_and_wait(joining)
            tab.leave()

        return leave

    def _to_page(
        self, session_id: str, messages: list[NotificationMessage]
    ) -> None:
        for message in messages:
            self._on_loop(
                self.session.emit,
                _Change(
                    _wire(notification_event(None, message)), to=session_id
                ),
            )

    def _close(
        self, subscription_id: str, reason: str, code: int = _CLOSED
    ) -> None:
        self._on_loop(
            self.session.terminate,
            subscription_id,
            ErrorData(code=code, message=reason, data={"reason": reason}),
        )

    def _on_loop(self, call: Callable[..., None], *args: Any) -> None:
        """Call `call` on the loop that serves the event.

        A session notifies its pages from wherever it runs, which may be
        another thread or, in tests, another loop.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is None or running is self._loop:
            call(*args)
        else:
            self._loop.call_soon_threadsafe(call, *args)
