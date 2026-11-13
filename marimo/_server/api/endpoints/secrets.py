# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo import _loggers
from marimo._runtime.commands import RefreshSecretsCommand
from marimo._secrets.secrets import write_secret
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.api.utils import (
    dispatch_control_request,
    enforce_consumer_capability,
)
from marimo._server.models.models import (
    BaseResponse,
    ListSecretKeysRequest,
    SuccessResponse,
)
from marimo._server.models.secrets import (
    CreateSecretRequest,
    DeleteSecretRequest,
)
from marimo._types.ids import ConsumerId
from marimo._utils.http import HTTPException

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


@tool("secrets_keys")
@requires("edit")
async def list_keys(
    *, request: Request, body: ListSecretKeysRequest
) -> SuccessResponse:
    """Ask the kernel to list the secrets it can see; it answers over the stream."""
    return await dispatch_control_request(request, body)


@tool("secrets_create")
@requires("edit")
async def create_secret(
    *, request: Request, body: CreateSecretRequest
) -> BaseResponse:
    """Store a secret with its provider and refresh the kernel's secrets."""
    app_state = AppState(request)
    session_id = app_state.require_current_session_id()

    command = RefreshSecretsCommand()
    enforce_consumer_capability(app_state, command)

    # Write to the provider
    write_secret(body, app_state.config_manager.get_config(hide_secrets=False))

    # Refresh the secrets
    app_state.require_current_session().put_control_request(
        command,
        from_consumer_id=ConsumerId(session_id),
    )
    return SuccessResponse(success=True)


@tool("secrets_delete")
@requires("edit")
async def delete_secret(
    *, request: Request, body: DeleteSecretRequest
) -> BaseResponse:
    """Delete a secret; not implemented yet."""
    del request, body
    raise HTTPException(status_code=501, detail="Not implemented")
