# Copyright 2026 Marimo. All rights reserved.
"""An editor or app session, followed with the host's events about its notebook."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.websockets import WebSocketDisconnect

from marimo._core import protocol
from marimo._core.stream import RoutedEvent
from marimo._messaging.notification import KernelStartupErrorNotification
from marimo._messaging.serde import deserialize_kernel_message
from marimo._server.api.endpoints.ws.notification_filter import (
    should_filter_operation,
)
from marimo._server.api.endpoints.ws.session_handler import SessionHandler
from marimo._server.codes import WebSocketCloseReason, WebSocketCodes
from marimo._server.host.events import (
    MarimoEvent,
    notification_event,
    serialize_event,
)
from marimo._session.managers.ipc import KernelStartupError
from marimo._utils.asyncio_utils import cancel_and_wait

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from starlette.requests import Request

    from marimo._core.notebook import Notebook
    from marimo._messaging.types import KernelMessage
    from marimo._server.api.endpoints.ws.ws_connection_validator import (
        ConnectionParams,
    )
    from marimo._server.host.bridge import SessionBridge
    from marimo._server.session_manager import SessionManager
    from marimo._session.session import Session


@dataclass(frozen=True)
class Close:
    """Ends a stream with a WebSocket close code and reason."""

    code: int
    reason: str


class SessionEventStream(SessionHandler):
    """Merge host events with the session's private notification delivery."""

    def __init__(
        self,
        connection: Request,
        *,
        bridge: SessionBridge,
        manager: SessionManager,
        params: ConnectionParams,
    ) -> None:
        super().__init__(
            manager=manager,
            params=params,
            mode=manager.mode,
        )
        self.connection = connection
        self._stream_finished = False
        self.notebook: Notebook | None = None
        self.bridge = bridge
        self._attachment: protocol.Attachment | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._ready_id = 0
        self._events: asyncio.Queue[RoutedEvent | Close | None] = (
            asyncio.Queue()
        )

    def notify(self, notification: KernelMessage) -> None:
        event = notification_event(
            self.notebook.id if self.notebook is not None else None,
            deserialize_kernel_message(notification),
        )
        self._events.put_nowait(event)

    def _on_host_event(self, event: RoutedEvent) -> None:
        # The session delivers marimo notifications with recipient filtering,
        # including edits that must not echo to the client that sent them.
        if not isinstance(event.event, MarimoEvent) and (
            event.position is None or event.position > self._ready_id
        ):
            self._events.put_nowait(event)

    async def _prepare_session(self, session: Session) -> None:
        notebook = await self.bridge.adopt(session)
        if notebook is None:
            raise WebSocketDisconnect(
                WebSocketCodes.UNEXPECTED_ERROR, "Notebook unavailable"
            )
        self._subscribe(notebook)

    def _subscribe(self, notebook: Notebook) -> None:
        self.notebook = notebook
        self._attachment = notebook.attach(identity=self.params.session_id)
        self._unsubscribe = self.bridge.host.subscribe(
            self._on_host_event, notebook_id=notebook.id
        )
        initial = self.bridge.host.events(None, notebook_id=notebook.id)
        self._ready_id = initial[-1].position or 0
        for event in initial:
            # Native replay must retain the session's recipient filtering.
            if not isinstance(event.event, MarimoEvent):
                self._events.put_nowait(event)

    async def stream(
        self,
    ) -> AsyncGenerator[RoutedEvent | Close, None]:
        """Broker the session and deliver its host events until it ends."""
        try:
            async with self._connection_lifetime(self.connection) as startup:

                async def connect_and_listen() -> None:
                    try:
                        await startup
                        self._check_status_update()
                        await self._wait_for_disconnect()
                    except asyncio.CancelledError:
                        return
                    except KernelStartupError as exc:
                        self._serialize_and_notify(
                            KernelStartupErrorNotification(error=str(exc))
                        )
                        self._request_close(
                            WebSocketCodes.UNEXPECTED_ERROR,
                            WebSocketCloseReason.KERNEL_STARTUP_ERROR,
                        )
                    except WebSocketDisconnect as exc:
                        self._request_close(exc.code, exc.reason or "")
                    finally:
                        self._events.put_nowait(None)

                listening = asyncio.create_task(connect_and_listen())
                try:
                    while True:
                        event = await self._events.get()
                        if event is None:
                            await listening
                            return
                        if isinstance(event, Close):
                            yield event
                            return
                        if isinstance(event.event, MarimoEvent):
                            viewer = self.params.kiosk
                            if self._session is not None:
                                viewer = self._is_viewer(self._session)
                            if should_filter_operation(
                                event.event.data.marimo.name, is_kiosk=viewer
                            ):
                                continue
                        yield event
                finally:
                    await cancel_and_wait(listening)
        finally:
            self._stream_finished = True
            if self._unsubscribe is not None:
                self._unsubscribe()
            if self.notebook is not None and self._attachment is not None:
                self.notebook.detach(self._attachment.id)

    def serialize(self, event: RoutedEvent) -> str:
        """An event as the text of one frame, with code when it may be sent."""
        return serialize_event(
            event, include_code=self.manager.should_send_code_to_frontend()
        )

    async def _wait_for_disconnect(self) -> None:
        # A subscription lasts until it is cancelled.
        await asyncio.Future()

    def _request_close(self, code: int, reason: str) -> None:
        self._events.put_nowait(Close(code, reason))

    def _is_transport_connected(self) -> bool:
        return not self._stream_finished
