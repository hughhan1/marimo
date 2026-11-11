# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING, cast

from starlette.authentication import requires

from marimo import _loggers
from marimo._config.config import PartialMarimoConfig
from marimo._config.config_schema import MarimoConfig
from marimo._config.resolver import config_paths
from marimo._config.schema_utils import config_leaf_types
from marimo._dependencies.dependencies import DependencyManager
from marimo._messaging.notification import MissingPackageAlertNotification
from marimo._runtime.commands import UpdateUserConfigCommand
from marimo._runtime.packages.utils import is_python_isolated
from marimo._server.ai.mcp.config import is_mcp_config_empty
from marimo._server.api.deps import AppState
from marimo._server.api.tools import after_reply, tool
from marimo._server.api.utils import enforce_consumer_capability
from marimo._server.lsp import any_lsp_server_running
from marimo._server.models.models import (
    BaseResponse,
    SaveUserConfigurationRequest,
    SuccessResponse,
)
from marimo._session import send_message_to_consumer
from marimo._types.ids import ConsumerId

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


@tool(
    "kernel_save_user_config",
    input=SaveUserConfigurationRequest,
    output=SuccessResponse,
)
@requires("edit")
async def save_user_config(
    *,
    request: Request,
    body: SaveUserConfigurationRequest,
) -> BaseResponse:
    """Save the user's configuration and apply it to the server and kernel."""
    app_state = AppState(request)
    session_id = app_state.get_current_session_id()
    session = app_state.get_current_session()
    # Refuse read-only connections before writing anything to disk.
    if session is not None:
        enforce_consumer_capability(
            app_state,
            UpdateUserConfigCommand(body.config),
        )

    # TODO: we may want to validate deep-partial here, but validating with PartialMarimoConfig it too strict
    # so we just cast to PartialMarimoConfig
    app_state.config_manager.save_config(
        cast(PartialMarimoConfig, body.config)
    )
    config_manager = (
        session.config_manager
        if session is not None
        else app_state.config_manager
    )
    config = config_manager.get_config(hide_secrets=False)

    async def handle_background_tasks() -> None:
        # Update the server's view of the config
        if any_lsp_server_running(config):
            LOGGER.debug("Starting language servers")
            await app_state.session_manager.start_lsp_server()

        # Reconfigure MCP servers if config changed
        mcp_config = config.mcp

        # Handle missing MCP dependencies
        if (
            not is_mcp_config_empty(mcp_config)
            and not DependencyManager.mcp.has()
        ):
            # If we're in an edit session, send an package installation request
            if session_id is not None and session is not None:
                send_message_to_consumer(
                    session=session,
                    operation=MissingPackageAlertNotification(
                        packages=["mcp"],
                        isolated=is_python_isolated(),
                    ),
                    consumer_id=ConsumerId(session_id),
                )

        try:
            from marimo._server.ai.mcp import get_mcp_client

            if mcp_config and not is_mcp_config_empty(mcp_config):
                LOGGER.debug("Reconfiguring MCP servers with updated config")
                mcp_client = get_mcp_client()
                await mcp_client.configure(mcp_config)
                LOGGER.info(
                    f"MCP servers reconfigured: {list(mcp_client.servers.keys())}"
                )
        except Exception as e:
            LOGGER.warning(f"Failed to reconfigure MCP servers: {e}")

    after_reply(request, handle_background_tasks)

    # Update the kernel's view of the config
    # Session could be None if the user is on the home page
    if session is not None:
        session.put_control_request(
            UpdateUserConfigCommand(
                config.to_dict(),
                edited_paths=tuple(
                    config_paths(
                        body.config,
                        leaf_paths=frozenset(config_leaf_types(MarimoConfig)),
                    )
                ),
            ),
            from_consumer_id=ConsumerId(
                app_state.require_current_session_id()
            ),
        )

    return SuccessResponse()
