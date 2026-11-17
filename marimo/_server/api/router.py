# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import Mount, Route

from marimo._server.api.endpoints.ai import router as ai_router
from marimo._server.api.endpoints.assets import router as assets_router
from marimo._server.api.endpoints.health import router as health_router
from marimo._server.api.endpoints.login import router as login_router
from marimo._server.api.endpoints.terminal import router as terminal_router
from marimo._server.api.tools import MCPEndpoint
from marimo._server.router import APIRouter

if TYPE_CHECKING:
    from starlette.routing import BaseRoute


# Define the app routes
def build_routes(endpoint: MCPEndpoint, base_url: str = "") -> list[BaseRoute]:
    app_router = APIRouter(prefix=base_url)
    app_router.include_router(ai_router, prefix="/api/ai", name="ai")
    app_router.include_router(login_router, prefix="/auth", name="auth")
    app_router.include_router(
        terminal_router, prefix="/terminal", name="terminal"
    )
    app_router.routes.append(
        Route(f"{base_url}/mcp", endpoint, methods=["POST"])
    )
    app_router.routes.append(
        Mount(f"{base_url}/mcp/files", endpoint.storage.app)
    )
    app_router.include_router(health_router, name="health")
    app_router.include_router(assets_router, name="assets")

    return app_router.routes
