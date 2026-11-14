# Copyright 2026 Marimo. All rights reserved.
"""A page's place in an editor or app session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.websockets import WebSocketDisconnect

from marimo._messaging.notification import KernelStartupErrorNotification
from marimo._messaging.serde import serialize_kernel_message
from marimo._server.api.endpoints.ws.session_handler import SessionHandler
from marimo._server.api.endpoints.ws.ws_session_connector import (
    SessionConnector,
)
from marimo._server.codes import WebSocketCloseReason, WebSocketCodes
from marimo._session.managers.ipc import KernelStartupError

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from marimo._core.notebook import Notebook
    from marimo._messaging.notification import NotificationMessage
    from marimo._messaging.types import KernelMessage
    from marimo._server.api.endpoints.ws.ws_connection_validator import (
        ConnectionParams,
    )
    from marimo._server.host.bridge import SessionBridge
    from marimo._server.session_manager import SessionManager


class Tab(SessionHandler):
    """A page in its session: its role, and what the session sends it alone.

    The page reads what the session broadcasts from the host's event log,
    so the room does not send it here. `sent` receives what the session
    sends this page alone. `closed` receives the reason when the session
    ends the page's place in it.
    """

    receives_broadcasts = False

    def __init__(
        self,
        *,
        bridge: SessionBridge,
        manager: SessionManager,
        params: ConnectionParams,
        sent: Callable[[KernelMessage], None],
        closed: Callable[[str], None],
    ) -> None:
        super().__init__(manager=manager, params=params, mode=manager.mode)
        self.bridge = bridge
        self.notebook: Notebook | None = None
        self._sent = sent
        self._closed = closed
        self._writing: Callable[[NotificationMessage], None] | None = None

    async def connect(
        self, connection: Request
    ) -> tuple[list[NotificationMessage], str | None]:
        """Join the session as the page asks.

        Returns what joining wrote to the page, kernel-ready and the
        replay, and the reason it was refused, if it was.
        """
        wrote: list[NotificationMessage] = []
        self._writing = wrote.append
        try:
            await SessionConnector(
                manager=self.manager,
                handler=self,
                params=self.params,
                connection=connection,
            ).connect()
        except KernelStartupError as exc:
            wrote.append(KernelStartupErrorNotification(error=str(exc)))
            return wrote, WebSocketCloseReason.KERNEL_STARTUP_ERROR
        except WebSocketDisconnect as exc:
            return wrote, exc.reason or ""
        finally:
            self._writing = None
        return wrote, None

    def leave(self) -> None:
        """Leave the session, which then waits out its grace period."""
        self._on_disconnect()

    def notify(self, notification: KernelMessage) -> None:
        self._sent(notification)

    def _serialize_and_notify(self, notification: NotificationMessage) -> None:
        if self._writing is not None:
            self._writing(notification)
        else:
            self.notify(serialize_kernel_message(notification))

    async def _prepare_session(self, session: object) -> None:
        self.notebook = await self.bridge.adopt(session)  # type: ignore[arg-type]
        if self.notebook is None:
            raise WebSocketDisconnect(
                WebSocketCodes.UNEXPECTED_ERROR, "Notebook unavailable"
            )

    def _request_close(self, code: int, reason: str) -> None:
        del code
        self._closed(reason)

    def _is_transport_connected(self) -> bool:
        return True
