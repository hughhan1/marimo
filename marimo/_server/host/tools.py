# Copyright 2026 Marimo. All rights reserved.
"""The host protocol's operations as MCP tools, served by the MCP SDK.

The host API serves them at its `/mcp` to the host record's token, and the
editor serves them at the same path behind its own sign-in. A tool's
arguments are its call's parts flattened into one object, its result is the
reply, and a failure is a problem carried as the error's text.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

import msgspec
import msgspec.json
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp.types.jsonrpc import INVALID_PARAMS
from starlette.requests import Request
from starlette.responses import JSONResponse

from marimo import __version__, _loggers
from marimo._core import protocol
from marimo._core.calls import CALLS, arguments_of, assemble, reply_type
from marimo._core.operations import is_problem, problem
from marimo._server.api.tools import (
    APP_ONLY,
    SCHEMA_REF,
    TRANSPORT_SECURITY,
    dedented,
    inline_schema,
    origin_allowed,
    tool_failure,
)
from marimo._server.host.auth import host_header_ok, is_loopback

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.server.context import ServerRequestContext
    from starlette.types import Receive, Scope, Send

    from marimo._core.operations import Reply

LOGGER = _loggers.marimo_logger()
_EDITOR = "marimo.host.from_editor"


@dataclass(frozen=True)
class HostTool:
    call: Any
    """The operation's call struct, whose parts' fields are the arguments."""
    output: Any
    """The reply's type, or None for an empty reply."""
    serve: Callable[[Request, Any], Awaitable[Reply]]
    """Takes the request and the call, and returns a reply or a problem."""
    catalog: bool = False
    """Whether only the host's token may call it."""


@cache
def tools() -> dict[str, HostTool]:
    """The host's tools, by operation id."""
    from marimo._server.host.api import CATALOG
    from marimo._server.host.notebook_api import serve

    return {
        **{
            str(kind.__struct_config__.tag): HostTool(
                kind, reply_type(kind), serve
            )
            for kind in CALLS
        },
        **CATALOG,
    }


def _description(call: Any) -> str:
    return inspect.cleandoc(call.__doc__ or "").split("\n\n", 1)[0]


@cache
def contract() -> list[dict[str, Any]]:
    """The host's tool definitions, as `tools/list` answers."""
    found = tools()
    kinds = list(
        dict.fromkeys(
            kind
            for tool in found.values()
            for kind in (arguments_of(tool.call), tool.output)
            if kind is not None
        )
    )
    schemas, components = msgspec.json.schema_components(
        kinds, ref_template=SCHEMA_REF
    )
    schema_of = dict(zip(kinds, schemas, strict=True))
    definitions: list[dict[str, Any]] = []
    for name, tool in sorted(found.items()):
        arguments = inline_schema(
            arguments_of(tool.call), schema_of, components
        )
        arguments.pop("title", None)
        arguments.pop("description", None)
        definitions.append(
            dedented(
                {
                    "name": name,
                    "description": _description(tool.call),
                    "inputSchema": {
                        **arguments,
                        "type": "object",
                        "properties": arguments.get("properties", {}),
                        "required": arguments.get("required", []),
                        "additionalProperties": False,
                    },
                    "_meta": APP_ONLY,
                    "outputSchema": inline_schema(
                        tool.output, schema_of, components
                    )
                    if tool.output is not None
                    else {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            )
        )
    return definitions


async def _call(
    request: Request, params: types.CallToolRequestParams
) -> types.CallToolResult:
    tool = tools().get(params.name)
    if tool is None:
        raise MCPError(
            code=INVALID_PARAMS, message=f"Unknown tool {params.name}"
        )
    if request.scope.get(_EDITOR):
        # Before the arguments are read, as the editor's own tools do.
        refused = _editor_refusal(request, tool)
        if refused is not None:
            return tool_failure(refused)
    try:
        call = assemble(tool.call, params.arguments or {})
    except msgspec.ValidationError as error:
        return tool_failure(problem("invalid-request", str(error)))
    try:
        reply = await tool.serve(request, call)
    except Exception:
        LOGGER.exception("The host failed on %s", params.name)
        return tool_failure(problem("internal", "The host failed"))
    if is_problem(reply):
        return tool_failure(reply)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{params.name}: ok")],
        structured_content={} if reply is None else msgspec.to_builtins(reply),
    )


def _editor_refusal(
    request: Request, tool: HostTool
) -> protocol.Problem | None:
    from marimo._server.host.notebook_api import editor_refusal

    if not tool.catalog:
        return editor_refusal(request, tool.call)
    # Only the host's token calls the catalog, and the host API answers it
    # only from this machine, at its own address.
    client = request.client
    if client is None or not is_loopback(client.host):
        return problem("forbidden", "The host API answers only this machine")
    port = request.app.state.host_context.port
    if not host_header_ok(request.headers.get("host"), port):
        return problem(
            "forbidden", "The host API answers only its own address"
        )
    return problem("unauthorized", "Send the host record's token as a bearer")


@cache
def mcp_server() -> Server[Any]:
    """The MCP server of the host's tools.

    The endpoints serve it, and `protocols/host/server.py` hands it to
    `holon types generate`, which reads the tools' contract from it.
    """

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: Any
    ) -> types.ListToolsResult:
        del ctx, params
        return types.ListToolsResult(
            tools=[
                types.Tool.model_validate(definition)
                for definition in contract()
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext[Any, Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        assert ctx.request is not None
        return await _call(ctx.request, params)

    def input_schema(name: str) -> dict[str, Any] | None:
        schema: dict[str, Any] | None = next(
            (
                definition["inputSchema"]
                for definition in contract()
                if definition["name"] == name
            ),
            None,
        )
        return schema

    return Server(
        "marimo",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=input_schema,
    )


class HostMCPEndpoint:
    """An ASGI endpoint serving the host's tools, stateless, as the SDK does."""

    def __init__(self, *, editor: bool) -> None:
        self.editor = editor

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if not origin_allowed(Request(scope, receive)):
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Origin not allowed"},
                },
                status_code=403,
            )
            await response(scope, receive, send)
            return
        if self.editor:
            scope[_EDITOR] = True
        # The manager is meant to live as long as the app, in a lifespan the
        # server's many test apps never enter. A stateless request shares
        # nothing with the next, so each gets a manager of its own.
        manager = StreamableHTTPSessionManager(
            mcp_server(),
            json_response=True,
            stateless=True,
            security_settings=TRANSPORT_SECURITY,
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)


host_mcp_endpoint = HostMCPEndpoint(editor=False)
"""Serves the host's tools to the host record's token."""

editor_host_mcp_endpoint = HostMCPEndpoint(editor=True)
"""Serves the host's tools to the signed-in editor."""
