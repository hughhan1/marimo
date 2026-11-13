# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from unittest.mock import MagicMock

from marimo._config.config_schema import MarimoConfig
from marimo._server.api.endpoints.ws.ws_connection_validator import (
    ConnectionParams,
    ConnectionRejection,
    parse_connection_params,
)
from marimo._server.codes import WebSocketCloseReason, WebSocketCodes


def _app_state(
    unique_file_key: str | None = "test.py",
) -> MagicMock:
    config: MarimoConfig = MarimoConfig.from_dict(
        {"runtime": {"auto_instantiate": True}}
    )
    app_state = MagicMock()
    workspace = app_state.session_manager.workspace
    workspace.get_unique_file_key.return_value = unique_file_key
    manager = app_state.config_manager_at_file.return_value
    manager.get_config.return_value = config
    return app_state


def test_missing_session_id_is_rejected() -> None:
    result = parse_connection_params(_app_state(), session_id=None)
    assert result == ConnectionRejection(
        WebSocketCodes.NORMAL_CLOSE, WebSocketCloseReason.NO_SESSION_ID
    )


def test_missing_file_key_is_rejected() -> None:
    result = parse_connection_params(
        _app_state(unique_file_key=None), session_id="123"
    )
    assert result == ConnectionRejection(
        WebSocketCodes.NORMAL_CLOSE, WebSocketCloseReason.NO_FILE_KEY
    )


def test_extracts_params() -> None:
    result = parse_connection_params(
        _app_state(), session_id="123", kiosk=True, query_params="x=1&x=2"
    )
    assert result == ConnectionParams(
        session_id="123",
        file_key="test.py",
        kiosk=True,
        auto_instantiate=True,
        query_params="x=1&x=2",
    )


def test_file_query_param_wins_over_unique_file_key() -> None:
    result = parse_connection_params(
        _app_state(), session_id="123", file="other.py"
    )
    assert isinstance(result, ConnectionParams)
    assert result.file_key == "other.py"
