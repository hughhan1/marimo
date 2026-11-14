# Copyright 2026 Marimo. All rights reserved.
"""The editor's API as MCP tools, served by the MCP SDK at `/api/mcp`.

A tool's arguments are the fields of its request, its result is its reply,
and a failure is a problem carried as the error's text. The SDK owns the
transport: the JSON-RPC envelope, the mirrored headers, the protocol version
and `server/discover`. The arguments are decoded into the request's type by
msgspec, which refuses what the type does not declare. Files travel by the
MCP files extension: uploads and replies that are files go to and from
`/api/mcp/files`, outside the tool calls. The host protocol's notebook tools
are served here too, and this module also builds the host API's server.
"""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from functools import cache, partial
from typing import TYPE_CHECKING, Any, TypeVar, get_type_hints

import msgspec
import msgspec.json
import msgspec.structs
from holon.ext.files import EXTENSION, FileServer, LocalStorage
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types.jsonrpc import INVALID_PARAMS
from starlette.requests import Request
from starlette.responses import JSONResponse

from marimo import __version__
from marimo._core import protocol
from marimo._core.operations import (
    PlainKind,
    is_problem,
    problem as make_problem,
)
from marimo._server.api.events import events
from marimo._server.errors import handle_error

if TYPE_CHECKING:
    import os
    from collections.abc import Awaitable, Callable, Mapping

    from holon.ext.events import Events
    from mcp.server.context import ServerRequestContext
    from starlette.types import Receive, Scope, Send

APP_ONLY = {"ui": {"visibility": ["app"]}}
SCHEMA_REF = "#/$defs/{name}"
_AFTER_REPLY = "marimo.after_reply"
TRANSPORT_SECURITY = TransportSecuritySettings(
    # The app's own middleware and origin policy decide which hosts and
    # origins may call; the SDK would otherwise apply a second rule.
    enable_dns_rebinding_protection=False
)


_MAX_INLINE_BYTES = 1024 * 1024


class FileResult(msgspec.Struct, frozen=True):
    """A reply that is a file, such as an export: its bytes or its path."""

    data: bytes | os.PathLike[str]
    media_type: str
    filename: str | None = None


@dataclass(frozen=True)
class Tool:
    handler: Callable[..., object]
    input: type[msgspec.Struct] | None
    output: object
    description: str = ""
    title: str | None = None


TOOLS: dict[str, Tool] = {}
F = TypeVar("F", bound="Callable[..., object]")


def tool(name: str) -> Callable[[F], F]:
    def decorate(handler: F) -> F:
        hints = get_type_hints(
            inspect.unwrap(handler), localns={"Request": Request}
        )
        output = hints["return"]
        TOOLS[name] = Tool(
            handler,
            hints.get("body"),
            None if output is type(None) else output,
            _doc(handler) or _doc(hints.get("body")),
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


# ---- arguments --------------------------------------------------------------


def _arguments(found: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
    """The keyword arguments an editor handler takes for a call."""
    known = (
        set()
        if found.input is None
        else {
            member.encode_name
            for member in msgspec.structs.fields(found.input)
        }
    )
    for key in arguments:
        if key not in known:
            raise msgspec.ValidationError(
                f"Object contains unknown field `{key}`"
            )
    if found.input is None:
        return {}
    return {
        "body": msgspec.convert(
            arguments, found.input, dec_hook=lambda kind, value: kind(value)
        )
    }


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


def _with_defs(
    schema: dict[str, Any], components: dict[str, Any]
) -> dict[str, Any]:
    found: dict[str, Any] = {}
    _refs(schema, components, found)
    return {**schema, **({"$defs": found} if found else {})}


def inline_schema(
    kind: Any,
    schema_of: dict[Any, Any],
    components: dict[str, Any],
) -> dict[str, Any]:
    """A type's schema with its root inlined, so its fields are the object's."""
    root = dict(schema_of[kind])
    if "$ref" in root:
        root = dict(components[root["$ref"].removeprefix("#/$defs/")])
    return _with_defs(root, components)


def schemas(tools: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Tool definitions, as `tools/list` answers them.

    Each tool has a `description`, an optional `title`, and its arguments'
    and result's types as `input` and `output`, or as `arguments` and
    `result`, as the host protocol's tools name them.
    """

    def kinds(found: Any) -> tuple[Any, Any]:
        if isinstance(found, Tool):
            return found.input, found.output
        return found.arguments, found.result

    types_ = list(
        dict.fromkeys(
            kind
            for found in tools.values()
            for kind in kinds(found)
            if kind not in (None, FileResult)
        )
    )
    schemas_, components = msgspec.json.schema_components(
        types_, ref_template=SCHEMA_REF
    )
    schema_of = dict(zip(types_, schemas_, strict=True))
    definitions: list[dict[str, Any]] = []
    for name, found in sorted(tools.items()):
        given, reply = kinds(found)
        if given is None:
            arguments: dict[str, Any] = {"type": "object", "properties": {}}
        else:
            arguments = inline_schema(given, schema_of, components)
            arguments.pop("title", None)
            arguments.pop("description", None)
        definition: dict[str, Any] = {
            "name": name,
            **({"title": found.title} if found.title else {}),
            "description": found.description,
            "inputSchema": {
                **arguments,
                "type": "object",
                "properties": arguments.get("properties", {}),
                "required": arguments.get("required", []),
                "additionalProperties": False,
            },
            "_meta": APP_ONLY,
        }
        if reply is not FileResult:
            definition["outputSchema"] = (
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }
                if reply is None
                else inline_schema(reply, schema_of, components)
            )
        definitions.append(dedented(definition))
    return definitions


@cache
def contract() -> list[dict[str, Any]]:
    """The editor's tool definitions: its own and the host's notebook tools."""
    from marimo._server.host.tools import definitions

    return sorted(
        [*schemas(TOOLS), *definitions(catalog=False)],
        key=lambda definition: definition["name"],
    )


def dedented(node: Any) -> Any:
    """The schema with each description's indentation removed.

    msgspec copies a docstring into its schema as written, and Python 3.13
    strips the indentation that 3.12 keeps, so the same declarations would
    otherwise describe themselves differently under each.
    """
    if isinstance(node, list):
        return [dedented(item) for item in node]
    if isinstance(node, dict):
        return {
            key: inspect.cleandoc(value)
            if key == "description" and isinstance(value, str)
            else dedented(value)
            for key, value in node.items()
        }
    return node


# ---- calls ------------------------------------------------------------------


async def _call(
    files: FileServer,
    ctx: ServerRequestContext[Any, Any],
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    request = ctx.request
    assert isinstance(request, Request)
    if not request.user.is_authenticated:
        # Before the arguments are read: `@requires` would refuse the call,
        # but only once the handler is called with them.
        return tool_result(
            params.name,
            make_problem("unauthorized", "Authorization header required"),
        )
    found = TOOLS.get(params.name)
    if found is None:
        from marimo._server.host.tools import editor_call, notebook_tools

        if params.name not in notebook_tools():
            raise MCPError(
                code=INVALID_PARAMS, message=f"Unknown tool {params.name}"
            )
        return tool_result(
            params.name,
            await editor_call(request, params.name, params.arguments or {}),
        )
    try:
        result = found.handler(
            request=request,
            **_arguments(found, params.arguments or {}),
        )
        if inspect.isawaitable(result):
            result = await result
    except msgspec.ValidationError as error:
        return tool_result(
            params.name, make_problem("invalid-request", str(error))
        )
    except Exception as error:  # noqa: BLE001
        return tool_result(params.name, await _problem_for(request, error))
    if isinstance(result, FileResult):
        return await files.reply(
            ctx,
            result.data,
            name=result.filename,
            mime_type=result.media_type,
            text=f"{params.name}: {result.filename or 'a file'}",
        )
    return tool_result(params.name, result)


def tool_result(name: str, reply: object) -> types.CallToolResult:
    """A reply as a tool's result, or a problem as its failure."""
    if is_problem(reply):
        return types.CallToolResult(
            is_error=True,
            content=[
                types.TextContent(
                    type="text", text=json.dumps(msgspec.to_builtins(reply))
                )
            ],
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{name}: ok")],
        structured_content={} if reply is None else msgspec.to_builtins(reply),
    )


_KINDS: dict[int, PlainKind] = {
    400: "invalid-request",
    401: "unauthorized",
    403: "forbidden",
    404: "not-found",
    405: "method-not-allowed",
    409: "conflict",
    501: "unsupported-operation",
}


async def _problem_for(request: Request, error: Exception) -> protocol.Problem:
    """The problem an editor handler's exception describes.

    The HTTP error handler already knows every exception's status and
    message, and sends the session the alerts some of them call for.
    """
    response = await handle_error(request, error)
    status = getattr(response, "status_code", 500)
    detail: Any = None
    body = getattr(response, "body", b"")
    if body:
        try:
            detail = json.loads(body).get("detail")
        except (ValueError, AttributeError):
            detail = body.decode(errors="replace")
    kind = _KINDS.get(
        status, "internal" if status >= 500 else "invalid-request"
    )
    return make_problem(kind, detail if detail is None else str(detail))


def tool_server(
    definitions: Callable[[], list[dict[str, Any]]],
    call: Callable[
        [ServerRequestContext[Any, Any], types.CallToolRequestParams],
        Awaitable[types.CallToolResult],
    ],
) -> Server[Any]:
    """An MCP server of tools: `definitions` lists them and `call` serves one."""

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: Any
    ) -> types.ListToolsResult:
        del ctx, params
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(each) for each in definitions()]
        )

    def input_schema(name: str) -> dict[str, Any] | None:
        return next(
            (
                definition["inputSchema"]
                for definition in definitions()
                if definition["name"] == name
            ),
            None,
        )

    return Server(
        "marimo",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call,
        get_tool_input_schema=input_schema,
    )


class MCPEndpoint:
    """An ASGI endpoint serving an MCP server and its events, stateless."""

    def __init__(
        self,
        server: Server[Any],
        events: Events,
    ) -> None:
        self.server = server
        self.events = events

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        request = Request(scope, receive)
        if not origin_allowed(request):
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
        manager = StreamableHTTPSessionManager(
            self.server,
            json_response=True,
            stateless=True,
            security_settings=TRANSPORT_SECURITY,
            # The routes took a body of any size, and `mo.ui.file` sends a
            # file's contents inside a UI element's value.
            max_request_body_size=sys.maxsize,
        )
        # Made here, since the events endpoint hands tools a copy of the scope.
        tasks = scope.setdefault(_AFTER_REPLY, [])
        async with manager.run():
            await self.events.asgi(manager)(scope, receive, send)
        for task in tasks:
            await task()


class EditorMCP(MCPEndpoint):
    def __init__(self) -> None:
        self.storage = LocalStorage()
        definitions = {
            definition["name"]: definition for definition in contract()
        }
        files = FileServer(
            self.storage,
            tools=definitions.get,
            max_inline_bytes=_MAX_INLINE_BYTES,
        )
        server = tool_server(contract, partial(_call, files))
        server.extensions[EXTENSION] = files.settings()
        server.middleware.insert(0, files)
        served = events()
        served.serve(server)
        super().__init__(server, served)


def origin_allowed(request: Request) -> bool:
    """Whether a browser at the request's Origin may call this server."""
    policy = getattr(request.app.state, "origin_policy", None)
    if policy is None:
        return True
    return bool(
        policy.allows(
            request.headers.getlist("origin"),
            request.headers.get("host", ""),
            request.url.scheme,
            request.scope,
        )
    )
