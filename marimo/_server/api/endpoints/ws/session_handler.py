# Copyright 2026 Marimo. All rights reserved.
"""Transport-agnostic session handling shared by WebSocket and SSE."""

from __future__ import annotations

import abc
import asyncio
from typing import TYPE_CHECKING

from marimo import _loggers
from marimo._cli.upgrade import check_for_updates
from marimo._config.settings import GLOBAL_SETTINGS
from marimo._messaging.notification import (
    AlertNotification,
    BannerNotification,
    NotificationMessage,
)
from marimo._messaging.serde import (
    serialize_kernel_message,
)
from marimo._server.api.endpoints.ws.ws_kernel_ready import build_kernel_ready
from marimo._server.codes import WebSocketCloseReason, WebSocketCodes
from marimo._session.consumer import SessionConsumer
from marimo._session.model import ConnectionState, SessionMode
from marimo._types.ids import ConsumerId

if TYPE_CHECKING:
    from marimo._config.cli_state import MarimoCLIState
    from marimo._messaging.types import KernelMessage
    from marimo._plugins.core.web_component import JSONType
    from marimo._server.api.endpoints.ws.ws_connection_validator import (
        ConnectionParams,
    )
    from marimo._server.session_manager import SessionManager
    from marimo._session import Session
    from marimo._session.events import SessionEventBus

LOGGER = _loggers.marimo_logger()


class SessionHandler(SessionConsumer, abc.ABC):
    """Base consumer that sessions use to send messages to frontends.

    Owns the transport-agnostic session lifecycle: connecting (new, resume,
    reconnect, kiosk), the kernel-ready handshake, message replay, and
    TTL-based session close on disconnect. Subclasses implement the small
    transport surface (`_request_close`, `_is_transport_connected`) and
    deliver notifications through the host stream.
    """

    def __init__(
        self,
        *,
        manager: SessionManager,
        params: ConnectionParams,
        mode: SessionMode,
    ):
        self.manager = manager
        self.params = params
        self.mode = mode
        self.status = ConnectionState.CONNECTING
        self._session: Session | None = None
        self.cancel_close_handle: asyncio.TimerHandle | None = None
        self._consumer_id = ConsumerId(params.session_id)

    @property
    def consumer_id(self) -> ConsumerId:
        return self._consumer_id

    # Transport surface, implemented per transport

    @abc.abstractmethod
    def _request_close(self, code: int, reason: str) -> None:
        """Ask the transport to close with the given code and reason.

        Must not block; called from synchronous session callbacks.
        """

    @abc.abstractmethod
    def _is_transport_connected(self) -> bool:
        """Whether the transport can still deliver messages."""

    # Shared session lifecycle

    async def _prepare_session(self, session: Session) -> None:
        """Prepare a selected session before delivering its initial state."""

    def _is_viewer(self, session: Session) -> bool:
        """Whether this consumer gets read-only (kiosk) message filtering.

        The viewer is whichever connection is not the room's main consumer;
        a takeover flips this by reassigning the main consumer.
        """
        return session.room.main_consumer is not self

    @abc.abstractmethod
    def notify(self, notification: KernelMessage) -> None:
        """Deliver a private session notification through the host stream."""

    def _serialize_and_notify(self, notification: NotificationMessage) -> None:
        self.notify(serialize_kernel_message(notification))

    def _write_kernel_ready_from_session_view(
        self, session: Session, kiosk: bool
    ) -> None:
        """Write kernel ready message using current session view state."""
        self._write_kernel_ready(
            session=session,
            resumed=True,
            ui_values=session.session_view.ui_values,
            kiosk=kiosk,
            auto_instantiated=False,
        )

    def _write_kernel_ready(
        self,
        session: Session,
        resumed: bool,
        ui_values: dict[str, JSONType],
        kiosk: bool,
        auto_instantiated: bool,
    ) -> None:
        """Communicates to the client that the kernel is ready.

        Sends cell code and other metadata to client.

        Args:
            session: Current session
            resumed: Whether this is a resumed session
            ui_values: UI element values
            kiosk: Whether this is kiosk mode
            auto_instantiated: Whether the kernel has already been instantiated
                server-side (run mode). If True, the frontend does not need
                to instantiate the app.
        """
        kernel_ready_msg = build_kernel_ready(
            session=session,
            resumed=resumed,
            ui_values=ui_values,
            kiosk=kiosk,
            auto_instantiated=auto_instantiated,
        )
        self._serialize_and_notify(kernel_ready_msg)

    def _reconnect_session(self, session: Session, replay: bool) -> None:
        """Reconnect to an existing session (kernel).

        A connection can be closed when a user's computer goes to sleep,
        spurious network issues, etc.
        """
        # Cancel previous close handle
        if self.cancel_close_handle is not None:
            self.cancel_close_handle.cancel()

        self.status = ConnectionState.OPEN
        session.connect_consumer(self, main=True)

        # If not replaying, just send a toast
        if not replay:
            self._serialize_and_notify(
                AlertNotification(
                    title="Reconnected",
                    description="You have reconnected to an existing session.",
                )
            )
            self._replay_previous_session(session)
            return

        self._write_kernel_ready_from_session_view(session, self.params.kiosk)
        self._serialize_and_notify(
            BannerNotification(
                title="Reconnected",
                description="You have reconnected to an existing session.",
                action="restart",
            )
        )

        self._replay_previous_session(session)

    def _connect_kiosk(self, session: Session) -> None:
        """Connect to a kiosk session.

        A kiosk session is a write-ish session that is connected to a
        frontend. It can set UI elements and interact with the sidebar,
        but cannot change or execute code. This is not a permission limitation,
        but rather we don't have full multi-player support yet.

        Kiosk mode is useful when the user is using an editor (VSCode or VIM)
        that does not easily support our reactive frontend or our panels.
        The user uses VSCode or VIM to write code, and the
        marimo kiosk/frontend to visualize the output.
        """

        session.connect_consumer(self, main=False)
        self.status = ConnectionState.CONNECTING
        self._write_kernel_ready_from_session_view(session, kiosk=True)
        self.status = ConnectionState.OPEN
        self._replay_previous_session(session)

    def _replay_previous_session(self, session: Session) -> None:
        """Replay retained state to this client."""
        notifications = session.session_view.snapshot_notifications()
        LOGGER.debug("Replaying %s notifications", len(notifications))
        for notif in notifications:
            LOGGER.debug("Replaying notification %s", notif)
            self._serialize_and_notify(notif)

    def _on_disconnect(self) -> None:
        session = self._session
        if session is None:
            return
        LOGGER.debug(
            "Connection closed for session %s",
            self.params.session_id,
        )

        # Change the status
        self.status = ConnectionState.CLOSED
        # Disconnect the consumer
        session.disconnect_consumer(self)

        # When the connection is closed, we wait session.ttl_seconds before
        # closing the session. This prevents the session from being closed
        # during intermittent network issues.
        # In RUN mode, this always applies (sessions always have a default
        # TTL even if the manager's ttl_seconds is None).
        # In EDIT mode, this only applies when --session-ttl is explicitly set.
        should_ttl_close = (
            self.manager.ttl_seconds is not None
            or self.mode == SessionMode.RUN
        )
        if should_ttl_close:
            connection_generation = session.connection_generation

            def _close() -> None:
                live = self.manager.get_session(self.params.session_id)
                # A later connection owns its own full retention window,
                # including while it is disconnected again.
                if (
                    live is None
                    or live is not session
                    or live.connection_generation != connection_generation
                ):
                    return
                if self.status != ConnectionState.OPEN:
                    # Guard: if another consumer has taken over, the session
                    # is alive.
                    if live.connection_state() == ConnectionState.OPEN:
                        LOGGER.debug(
                            "Session %s has active consumer, skipping TTL close",
                            self.params.session_id,
                        )
                        return
                    LOGGER.debug(
                        "Closing session %s (TTL EXPIRED)",
                        self.params.session_id,
                    )
                    self.manager.close_session(self.params.session_id)

            self.cancel_close_handle = asyncio.get_running_loop().call_later(
                session.ttl_seconds, _close
            )

    def on_attach(self, session: Session, event_bus: SessionEventBus) -> None:
        del event_bus
        self._session = session

    def on_detach(self) -> None:
        self._session = None
        # If the transport is open, send a close message
        is_connected = (
            self.status == ConnectionState.OPEN
            or self.status == ConnectionState.CONNECTING
        ) and self._is_transport_connected()
        if is_connected:
            self._request_close(
                WebSocketCodes.NORMAL_CLOSE, WebSocketCloseReason.SHUTDOWN
            )

    def connection_state(self) -> ConnectionState:
        return self.status

    def _check_status_update(self) -> None:
        # Only check for updates if we're in edit mode
        if (
            not GLOBAL_SETTINGS.CHECK_STATUS_UPDATE
            or self.mode != SessionMode.EDIT
        ):
            return

        loop = asyncio.get_running_loop()

        def on_update(current_version: str, state: MarimoCLIState) -> None:
            # Runs on the worker thread; hop back to the event loop to
            # notify (the message queue is not thread-safe).

            # Let's only toast once per marimo server
            # so we can just store this in memory.
            # We still want to check for updates (which are debounced 24 hours)
            # but don't keep toasting.
            global has_toasted
            if has_toasted:
                return

            has_toasted = True

            title = (
                f"Update available {current_version} → {state.latest_version}"
            )
            release_url = "https://github.com/marimo-team/marimo/releases"

            # Build description with notices if present
            description = f"Check out the <a class='underline' target='_blank' href='{release_url}'>latest release on GitHub.</a>"

            if state.notices:
                notices_text = (
                    "<br><br><strong>Recent updates:</strong><br>"
                    + "<br>".join(f"• {notice}" for notice in state.notices)
                )
                description += notices_text

            loop.call_soon_threadsafe(
                self._serialize_and_notify,
                AlertNotification(title=title, description=description),
            )

        # check_for_updates does synchronous network I/O; run it off the
        # event loop so it never delays kernel messages.
        task = asyncio.create_task(
            asyncio.to_thread(check_for_updates, on_update)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


has_toasted = False

# Strong refs so fire-and-forget tasks aren't GC'd mid-flight.
_background_tasks: set[asyncio.Task[None]] = set()
