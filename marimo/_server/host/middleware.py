# Copyright 2026 Marimo. All rights reserved.
"""Local access and bearer-token authentication for the host API.

Host bearer requests must come from loopback and address a loopback name
on the server's port. The bearer token comes from the host discovery record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.routing import Match, Route

from marimo import _loggers
from marimo._server.host import HOST_API_PATH
from marimo._server.host.api import build_app
from marimo._server.host.auth import (
    bearer_matches,
    host_header_ok,
    is_loopback,
)
from marimo._server.host.notebook_api import problem, status_problem

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.responses import Response
    from starlette.routing import BaseRoute
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER = _loggers.marimo_logger()


class HostApiMiddleware:
    """Authenticate and route host API requests."""

    def __init__(self, app: ASGIApp, *, base_url: str = "") -> None:
        self.app = app
        self.prefix = base_url + HOST_API_PATH
        self.host_app = build_app()

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http" or not self._is_host_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        request = HTTPConnection(scope)
        client = scope.get("client")
        context = getattr(request.app.state, "host_context", None)
        if context is None:
            response = problem("not-found", "This server is not a host")
        elif context.bridge.app_sessions is not None:
            response = problem(
                "not-found", "App sessions do not expose a host catalog"
            )
        elif client is None or not is_loopback(client[0]):
            response = problem(
                "forbidden", "The host API answers only this machine"
            )
        elif not host_header_ok(request.headers.get("host"), context.port):
            # Reject remote domain names to prevent DNS rebinding attacks.
            response = problem(
                "forbidden", "The host API answers only its own address"
            )
        elif not bearer_matches(
            request.headers.get("Authorization"), context.token
        ):
            response = problem(
                "unauthorized", "Send the host record's token as a bearer"
            )
            response.headers["WWW-Authenticate"] = "Bearer"
        else:
            scope = {**scope, "root_path": self.prefix}
            started = False

            async def guarded(message: Message) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                await send(message)

            try:
                await self.host_app(scope, receive, guarded)
            except HTTPException as exc:
                if started:
                    raise
                # The bare router has no exception middleware, so a known
                # path with an unsupported method surfaces here as a 405.
                response = _router_problem(exc, scope, self.host_app.routes)
                await response(scope, receive, send)
            except Exception:
                if started:
                    # The server logs the failure once it ends the stream.
                    raise
                LOGGER.exception("The host API failed on %s", scope["path"])
                response = problem("internal", "The host failed")
                await response(scope, receive, send)
            return
        await response(scope, receive, send)

    def _is_host_path(self, path: str) -> bool:
        return path == self.prefix or path.startswith(self.prefix + "/")


def _router_problem(
    exc: HTTPException, scope: Scope, routes: Sequence[BaseRoute]
) -> Response:
    response = status_problem(exc.status_code, exc.detail)
    if exc.status_code == 405:
        # The router names only the first route that matched the path, but
        # a path can be declared once per method.
        response.headers["Allow"] = ", ".join(
            sorted(_methods_declared_for(scope, routes))
        )
    return response


def _methods_declared_for(
    scope: Scope, routes: Sequence[BaseRoute]
) -> set[str]:
    methods: set[str] = set()
    for route in routes:
        if (
            isinstance(route, Route)
            and route.matches(scope)[0] is not Match.NONE
        ):
            methods.update(route.methods or ())
    return methods
