# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from marimo._cli.tips import CliTip
from marimo._config.manager import MarimoConfigManager, ScriptConfigReader
from marimo._core import protocol
from marimo._server.config import StarletteServerState
from marimo._server.session_manager import SessionManager
from marimo._server.tokens import SkewProtectionToken
from marimo._session.model import SessionMode
from marimo._types.ids import ConsumerId, SessionId
from marimo._utils.http import HTTPException, HTTPStatus

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.datastructures import State
    from starlette.requests import Request
    from starlette.websockets import WebSocket
    from uvicorn import Server

    from marimo._core.notebook import Notebook
    from marimo._server.host.context import HostContext
    from marimo._session import Session

TAB_HEADER: Final = "Marimo-Session-Id"
_SESSION_ID: Final = "marimo.session_id"


class AppStateBase:
    """The app state."""

    @staticmethod
    def from_request(request: Request | WebSocket) -> AppState:
        """Get the app state with a request."""
        return AppState(request)

    @staticmethod
    def from_app(asgi: Starlette) -> AppStateBase:
        """Get the app state with an ASGIApp app."""
        return AppStateBase(cast(Any, asgi).state)

    def __init__(self, state: State) -> None:
        """Initialize the app state."""
        self.state = cast(StarletteServerState, state)

    @property
    def session_manager(self) -> SessionManager:
        return self.state.session_manager

    @property
    def host_context(self) -> HostContext | None:
        return getattr(self.state, "host_context", None)

    def notebook_for_session(self, session: Session) -> Notebook | None:
        """Return the host notebook for an edit session, if available."""
        context = self.host_context
        return (
            None
            if context is None
            else context.bridge.notebook_for_session(session)
        )

    @property
    def mode(self) -> SessionMode:
        return self.session_manager.mode

    @property
    def quiet(self) -> bool:
        return self.state.quiet

    @property
    def host(self) -> str:
        return self.state.host

    @property
    def port(self) -> int:
        return self.state.port

    @property
    def maybe_port(self) -> int | None:
        return getattr(self.state, "port", None)

    @property
    def base_url(self) -> str:
        return self.state.base_url

    @property
    def server(self) -> Server:
        return self.state.server

    @property
    def config_manager(self) -> MarimoConfigManager:
        return self.state.config_manager

    @property
    def headless(self) -> bool:
        return self.state.headless

    @property
    def skew_protection_token(self) -> SkewProtectionToken:
        return self.session_manager.skew_protection_token

    @property
    def remote_url(self) -> str | None:
        if hasattr(self.state, "remote_url"):
            return self.state.remote_url
        return None

    @property
    def asset_url(self) -> str | None:
        if hasattr(self.state, "asset_url"):
            return self.state.asset_url
        return None

    @property
    def mcp_url(self) -> str | None:
        return getattr(self.state, "mcp_url", None)

    @property
    def enable_auth(self) -> bool:
        if hasattr(self.state, "enable_auth"):
            return self.state.enable_auth
        return True

    @property
    def startup_tip(self) -> CliTip | None:
        startup_tip = getattr(self.state, "startup_tip", None)
        return cast(CliTip | None, startup_tip)

    @property
    def html_head(self) -> str | None:
        if hasattr(self.state, "html_head"):
            return cast(str | None, self.state.html_head)
        return None


class AppState(AppStateBase):
    """The app state with a request."""

    def __init__(self, request: Request | WebSocket) -> None:
        """Initialize the app state with a request."""
        super().__init__(request.app.state)
        self.request = request

    @property
    def tab(self) -> ConsumerId | None:
        """The page tab that sent the request, which only a page names."""
        return cast(ConsumerId | None, self.request.headers.get(TAB_HEADER))

    def open_notebook(self, notebook_id: protocol.NotebookId) -> None:
        """Serve the request with the session running `notebook_id`."""
        context = self.host_context
        if context is None or notebook_id not in context.bridge.host.notebooks:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"No notebook {notebook_id}",
            )
        session = context.bridge.session_for_notebook(notebook_id)
        session_id = (
            None
            if session is None
            else self.session_manager.get_session_id(session)
        )
        if session_id is None:
            raise HTTPException(
                status_code=409,
                detail="The notebook has no running session.",
            )
        self.request.scope[_SESSION_ID] = session_id

    def get_current_session_id(self) -> SessionId | None:
        """The session of the notebook the request names, if it names one."""
        return cast(SessionId | None, self.request.scope.get(_SESSION_ID))

    def require_current_session_id(self) -> SessionId:
        session_id = self.get_current_session_id()
        if session_id is None:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail="Name the notebook with notebook_id",
            )
        return session_id

    def get_current_session(self) -> Session | None:
        session_id = self.get_current_session_id()
        return (
            None
            if session_id is None
            else self.session_manager.get_session(session_id)
        )

    def require_current_session(self) -> Session:
        session = self.session_manager.get_session(
            self.require_current_session_id()
        )
        if session is None:
            raise HTTPException(
                status_code=409,
                detail="The notebook has no running session.",
            )
        return session

    def require_query_params(self, param: str) -> str:
        """Get a query parameter or raise an error."""
        value = self.request.query_params[param]
        if not value:
            raise ValueError(f"Missing query parameter: {param}")
        return value

    def query_params(self, param: str) -> str | None:
        """Get a query parameter."""
        if param not in self.request.query_params:
            return None
        return self.request.query_params[param]

    # Config manager for the marimo file that we are running.
    # This could have custom config in the script metadata.
    @property
    def app_config_manager(self) -> MarimoConfigManager:
        session = self.require_current_session()
        return session.config_manager

    # We may have not created a session yet, but we know the file where we will
    # create one.
    # Use this file to override the config manager.
    def config_manager_at_file(self, path: str) -> MarimoConfigManager:
        config_manager = super().config_manager
        # The file key can be relative to the workspace directory, so resolve
        # it to a validated absolute path before reading inline script
        # metadata — otherwise ScriptConfigReader would read relative to the
        # server's cwd. New/unsaved or missing files have no inline config to
        # apply; let other rejections (e.g. path traversal) propagate.
        try:
            resolved = self.session_manager.workspace.resolve(path)
        except HTTPException as e:
            if e.status_code == HTTPStatus.NOT_FOUND:
                return config_manager
            raise
        if resolved is None:
            return config_manager
        return config_manager.with_partial(ScriptConfigReader(resolved))
