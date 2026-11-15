# Copyright 2026 Marimo. All rights reserved.
"""The editor's JSON API as MCP tools, served by the MCP SDK at `/api/mcp`.

A tool's arguments are the fields of its request, its result is its reply,
and a failure is a problem carried as the error's text. The SDK owns the
transport: the JSON-RPC envelope, the mirrored headers and the protocol
version. Arguments are decoded as the HTTP routes decoded their bodies.
"""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, TypeVar, get_type_hints

import msgspec
import msgspec.json
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types.jsonrpc import INVALID_PARAMS
from starlette.authentication import requires as scope_required
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from marimo import __version__
from marimo._server.errors import handle_error
from marimo._utils.parse_dataclass import parse_raw

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.server.context import ServerRequestContext
    from starlette.types import Receive, Scope, Send

SCHEMA_REF = "#/$defs/{name}"
_AFTER_REPLY = "marimo.after_reply"
TRANSPORT_SECURITY = TransportSecuritySettings(
    # The app's origin policy decides which browser origins may call.
    enable_dns_rebinding_protection=False
)


@dataclass(frozen=True)
class Tool:
    call: Callable[..., Awaitable[object]]
    input: type[Any] | None
    output: object
    description: str


TOOLS: dict[str, Tool] = {}
F = TypeVar("F", bound="Callable[..., object]")


def tool(
    name: str, *, requires: str, allow_unknown_keys: bool = False
) -> Callable[[F], F]:
    """Serve an endpoint handler as the tool `name`, to callers with `requires`.

    The handler takes `request` and, when the tool has arguments, `body`,
    typed as the request it decodes. Its return annotation is the result.
    The caller's scope is checked before the arguments are decoded, and
    `allow_unknown_keys` decodes them as `parse_request` does.
    """

    def decorate(handler: F) -> F:
        hints = get_type_hints(handler, localns={"Request": Request})
        body_type = hints.get("body")

        @scope_required(requires)
        async def call(request: Request, arguments: dict[str, Any]) -> object:
            body = (
                {}
                if body_type is None
                else {
                    "body": parse_raw(
                        arguments,
                        cls=body_type,
                        allow_unknown_keys=allow_unknown_keys,
                    )
                }
            )
            if inspect.iscoroutinefunction(handler):
                return await handler(request=request, **body)
            return await run_in_threadpool(handler, request=request, **body)

        output = hints["return"]
        TOOLS[name] = Tool(
            call,
            body_type,
            None if output is type(None) else output,
            _doc(handler) or _doc(body_type),
        )
        return handler

    return decorate


def _doc(target: Any) -> str:
    """The first paragraph of the target's own docstring, not an inherited one."""
    if target is None:
        return ""
    doc = target.__doc__ if inspect.isclass(target) else inspect.getdoc(target)
    if not doc:
        return ""
    return inspect.cleandoc(doc).split("\n\n", 1)[0].replace("\n", " ")


def after_reply(request: Request, task: Callable[[], Awaitable[None]]) -> None:
    """Run `task` once the call's reply is sent, as a background task."""
    request.scope.setdefault(_AFTER_REPLY, []).append(task)


# ---- schemas ----------------------------------------------------------------


def _refs(
    node: Any, components: dict[str, Any], found: dict[str, Any]
) -> None:
    if isinstance(node, list):
        for item in node:
            _refs(item, components, found)
    elif isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            if name not in found:
                found[name] = components[name]
                _refs(components[name], components, found)
        for value in node.values():
            _refs(value, components, found)


def _inline_schema(
    kind: Any, schema_of: dict[Any, Any], components: dict[str, Any]
) -> dict[str, Any]:
    """A type's schema with its root inlined, so its fields are the object's."""
    root = dict(schema_of[kind])
    if "$ref" in root:
        root = dict(components[root["$ref"].removeprefix("#/$defs/")])
    found: dict[str, Any] = {}
    _refs(root, components, found)
    return {**root, **({"$defs": found} if found else {})}


@cache
def contract() -> list[dict[str, Any]]:
    """The tools' definitions, as `tools/list` answers them."""
    kinds = list(
        dict.fromkeys(
            kind
            for found in TOOLS.values()
            for kind in (found.input, found.output)
            if kind is not None
        )
    )
    schemas, components = msgspec.json.schema_components(
        kinds, ref_template=SCHEMA_REF
    )
    schema_of = dict(zip(kinds, schemas, strict=True))
    definitions: list[dict[str, Any]] = []
    for name, found in sorted(TOOLS.items()):
        arguments: dict[str, Any] = (
            {}
            if found.input is None
            else _inline_schema(found.input, schema_of, components)
        )
        arguments.pop("title", None)
        arguments.pop("description", None)
        output: dict[str, Any] = (
            {"type": "object", "properties": {}, "additionalProperties": False}
            if found.output is None
            else _inline_schema(found.output, schema_of, components)
        )
        definitions.append(
            {
                "name": name,
                "description": found.description,
                "inputSchema": {
                    **arguments,
                    "type": "object",
                    "properties": arguments.get("properties", {}),
                    "required": arguments.get("required", []),
                    "additionalProperties": False,
                },
                "outputSchema": output,
            }
        )
    return definitions


# ---- calls ------------------------------------------------------------------


async def _call(
    ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
) -> types.CallToolResult:
    request = ctx.request
    assert isinstance(request, Request)
    found = TOOLS.get(params.name)
    if found is None:
        raise MCPError(
            code=INVALID_PARAMS, message=f"Unknown tool {params.name}"
        )
    try:
        result = await found.call(
            request=request, arguments=params.arguments or {}
        )
    except Exception as error:  # noqa: BLE001
        return await _failure(request, error)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{params.name}: ok")],
        structured_content={}
        if result is None
        else msgspec.to_builtins(result),
    )


async def _failure(request: Request, error: Exception) -> types.CallToolResult:
    """The failure an exception describes: the routes' error response as text.

    The text is the JSON the HTTP error handler would have answered with,
    plus its `status`, so a caller can branch on it as it did on the
    route's status code.
    """
    response = await handle_error(request, error)
    status = getattr(response, "status_code", 500)
    detail = _detail(getattr(response, "body", b""))
    return types.CallToolResult(
        is_error=True,
        content=[
            types.TextContent(
                type="text",
                text=json.dumps({"status": status, "detail": detail}),
            )
        ],
    )


def _detail(body: bytes | memoryview) -> Any:
    if not body:
        return None
    try:
        return json.loads(bytes(body)).get("detail")
    except (ValueError, AttributeError):
        return bytes(body).decode(errors="replace")


def mcp_server() -> Server[Any]:
    """The editor's tools as an MCP server."""

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: Any
    ) -> types.ListToolsResult:
        del ctx, params
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(each) for each in contract()]
        )

    return Server(
        "marimo",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=_call,
    )


class EditorMCP:
    """The ASGI endpoint that serves the editor's tools, stateless."""

    def __init__(self) -> None:
        self.server = mcp_server()

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        manager = StreamableHTTPSessionManager(
            self.server,
            stateless=True,
            security_settings=TRANSPORT_SECURITY,
            # The routes took a body of any size, and `mo.ui.file` sends a
            # file's contents inside a UI element's value.
            max_request_body_size=sys.maxsize,
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)
        for task in scope.get(_AFTER_REPLY, ()):
            await task()
