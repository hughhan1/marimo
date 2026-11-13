# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from dataclasses import dataclass

from marimo._server.api.deps import AppState
from marimo._server.codes import WebSocketCloseReason, WebSocketCodes
from marimo._server.workspace import MarimoFileKey
from marimo._types.ids import SessionId


@dataclass
class ConnectionParams:
    """Parameters extracted from a session connection request."""

    session_id: SessionId
    file_key: MarimoFileKey
    kiosk: bool
    auto_instantiate: bool
    query_params: str = ""


@dataclass
class ConnectionRejection:
    """Why a session connection request was refused.

    Carries the WebSocket close code and reason; non-websocket transports
    translate these into their own close signal.
    """

    code: WebSocketCodes
    reason: WebSocketCloseReason


def parse_connection_params(
    app_state: AppState,
    *,
    session_id: str | None,
    file: str | None = None,
    kiosk: bool = False,
    query_params: str = "",
) -> ConnectionParams | ConnectionRejection:
    """Validate the session arguments against the app's workspace and config."""
    if session_id is None:
        return ConnectionRejection(
            WebSocketCodes.NORMAL_CLOSE, WebSocketCloseReason.NO_SESSION_ID
        )

    session_id = SessionId(session_id)

    file_key: MarimoFileKey | None = (
        file or app_state.session_manager.workspace.get_unique_file_key()
    )

    if file_key is None:
        return ConnectionRejection(
            WebSocketCodes.NORMAL_CLOSE, WebSocketCloseReason.NO_FILE_KEY
        )

    config = app_state.config_manager_at_file(file_key).get_config()
    auto_instantiate = config.runtime.auto_instantiate

    return ConnectionParams(
        session_id=session_id,
        file_key=file_key,
        kiosk=kiosk,
        auto_instantiate=auto_instantiate,
        query_params=query_params,
    )
