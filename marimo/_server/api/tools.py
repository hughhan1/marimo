# Copyright 2026 Marimo. All rights reserved.
"""The editor's API as MCP tools, served by the MCP SDK at `/api/mcp`.

A tool's arguments are the fields of its request, its result is its reply,
and a failure is a problem carried as the error's text. The SDK owns the
transport: the JSON-RPC envelope, the mirrored headers, the protocol version
and `server/discover`. The arguments are decoded into the request's type by
msgspec, which refuses what the type does not declare. Files travel by the
MCP files extension: uploads and replies that are files go to and from
`/api/mcp/files`, outside the tool calls.
"""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

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
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from marimo import __version__
from marimo._core import protocol
from marimo._core.operations import PlainKind, problem as make_problem
from marimo._server.api.events import events, request_var
from marimo._server.errors import handle_error

if TYPE_CHECKING:
    import os
    from collections.abc import Awaitable, Callable

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
    name: str
    handler: Callable[..., Any]
    input: Any = None
    """The type whose fields are the arguments, or None for a tool with none."""
    output: Any = None
    """The reply's type, or None for an empty reply."""
    ids: tuple[str, ...] = ()
    """String arguments besides the input's fields, such as a record's id."""
    description: str = ""


TOOLS: dict[str, Tool] = {}


def tool(
    name: str,
    *,
    input: Any = None,  # noqa: A002
    output: Any = None,
    ids: tuple[str, ...] = (),
    description: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Serve an editor handler as a tool.

    The handler takes `request`, the endpoint's request, and `body`, the
    decoded `input`, by keyword, plus each id in `ids` as a string. It returns
    an `output` instance or a `FileResult`.
    """

    def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[name] = Tool(
            name,
            handler,
            input,
            output,
            ids,
            description or _doc(handler) or _doc(input),
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


@cache
def _server_filled(kind: Any) -> frozenset[str]:
    """The input's fields the server fills from the call, by wire name.

    A request struct may carry the HTTP request the kernel reports as the
    caller's context. A caller must not supply it.
    """
    if kind is None:
        return frozenset()
    return frozenset(
        member.encode_name
        for member in msgspec.structs.fields(kind)
        if "HTTPRequest" in str(member.type)
    )


def _arguments(
    request: Request, found: Tool, arguments: dict[str, Any]
) -> dict[str, Any]:
    """The keyword arguments an editor handler takes for a call."""
    given = dict(arguments)
    known = set(found.ids)
    if found.input is not None:
        known.update(
            member.encode_name
            for member in msgspec.structs.fields(found.input)
        )
        known -= _server_filled(found.input)
    for key in given:
        if key not in known:
            raise msgspec.ValidationError(
                f"Object contains unknown field `{key}`"
            )
    keywords: dict[str, Any] = {}
    for key in found.ids:
        if key not in given:
            raise msgspec.ValidationError(
                f"Object missing required field `{key}`"
            )
        keywords[key] = msgspec.convert(given.pop(key), str)
    if found.input is not None:
        body = msgspec.convert(given, found.input)
        filled = _server_filled(found.input)
        if filled:
            from marimo._runtime.commands import HTTPRequest

            for member in msgspec.structs.fields(found.input):
                if member.encode_name in filled:
                    setattr(
                        body, member.name, HTTPRequest.from_request(request)
                    )
        keywords["body"] = body
    return keywords


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
    omit: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """A type's schema with its root inlined, so its fields are the object's."""
    root = dict(schema_of[kind])
    if "$ref" in root:
        root = dict(components[root["$ref"].removeprefix("#/$defs/")])
    if omit:
        root["properties"] = {
            key: value
            for key, value in root.get("properties", {}).items()
            if key not in omit
        }
        if "required" in root:
            root["required"] = [k for k in root["required"] if k not in omit]
    return _with_defs(root, components)


@cache
def contract() -> list[dict[str, Any]]:
    """The editor's tool definitions, as `tools/list` answers."""
    kinds = list(
        dict.fromkeys(
            kind
            for found in TOOLS.values()
            for kind in (found.input, found.output)
            if kind not in (None, FileResult)
        )
    )
    schemas, components = msgspec.json.schema_components(
        kinds, ref_template=SCHEMA_REF
    )
    schema_of = dict(zip(kinds, schemas, strict=True))
    definitions: list[dict[str, Any]] = []
    for name, found in sorted(TOOLS.items()):
        if found.input is None:
            arguments: dict[str, Any] = {"type": "object", "properties": {}}
        else:
            arguments = inline_schema(
                found.input,
                schema_of,
                components,
                _server_filled(found.input),
            )
            arguments.pop("title", None)
            arguments.pop("description", None)
        properties = {
            **{key: {"type": "string"} for key in found.ids},
            **arguments.get("properties", {}),
        }
        definition: dict[str, Any] = {
            "name": name,
            "description": found.description,
            "inputSchema": {
                **arguments,
                "type": "object",
                "properties": properties,
                "required": [*found.ids, *arguments.get("required", [])],
                "additionalProperties": False,
            },
            "_meta": APP_ONLY,
        }
        output = _output_schema(found, schema_of, components)
        if output is not None:
            definition["outputSchema"] = output
        definitions.append(dedented(definition))
    return definitions


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


def _output_schema(
    found: Tool, schema_of: dict[Any, Any], components: dict[str, Any]
) -> dict[str, Any] | None:
    if found.output is FileResult:
        return None
    if found.output is None:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    return inline_schema(found.output, schema_of, components)


def _models() -> list[types.Tool]:
    return [types.Tool.model_validate(definition) for definition in contract()]


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
        if not request.user.is_authenticated:
            # Before the arguments are read: `@requires` would refuse the
            # call, but only once the handler is called with them.
            raise HTTPException(status_code=403)
        result = found.handler(
            request=request,
            **_arguments(request, found, params.arguments or {}),
        )
        if inspect.isawaitable(result):
            result = await result
    except msgspec.ValidationError as error:
        return tool_failure(make_problem("invalid-request", str(error)))
    except Exception as error:  # noqa: BLE001
        return tool_failure(await _problem_for(request, error))
    if isinstance(result, FileResult):
        return await files().reply(
            ctx,
            result.data,
            name=result.filename,
            mime_type=result.media_type,
            text=f"{found.name}: {result.filename or 'a file'}",
        )
    return _success(found, result)


def _success(found: Tool, result: Any) -> types.CallToolResult:
    summary = types.TextContent(type="text", text=f"{found.name}: ok")
    return types.CallToolResult(
        content=[summary],
        structured_content={}
        if result is None
        else msgspec.to_builtins(result),
    )


def tool_failure(failure: protocol.Problem) -> types.CallToolResult:
    return types.CallToolResult(
        is_error=True,
        content=[
            types.TextContent(
                type="text", text=json.dumps(msgspec.to_builtins(failure))
            )
        ],
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


# ---- files ------------------------------------------------------------------


@cache
def file_storage() -> LocalStorage:
    """Where uploads wait for their call and file replies are served.

    Its app is mounted at `/api/mcp/files`, under the tool endpoint, where
    the targets it issues point.
    """
    return LocalStorage()


@cache
def files() -> FileServer:
    """The tools' file arguments and file replies."""
    definitions = {definition["name"]: definition for definition in contract()}
    return FileServer(
        file_storage(),
        tools=definitions.get,
        max_inline_bytes=_MAX_INLINE_BYTES,
    )


# ---- endpoint ---------------------------------------------------------------


@cache
def mcp_server() -> Server[Any]:
    """The MCP server of the editor's tools.

    The endpoint serves it, and `packages/openapi/server.py` hands it to
    `holon types generate`, which reads the tools' contract from it.
    """

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: Any
    ) -> types.ListToolsResult:
        del ctx, params
        return types.ListToolsResult(tools=_models())

    async def call_tool(
        ctx: ServerRequestContext[Any, Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        return await _call(ctx, params)

    def input_schema(name: str) -> dict[str, Any] | None:
        if name not in TOOLS:
            return None
        schema: dict[str, Any] = next(
            definition["inputSchema"]
            for definition in contract()
            if definition["name"] == name
        )
        return schema

    server = Server(
        "marimo",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=input_schema,
    )
    server.extensions[EXTENSION] = files().settings()
    server.middleware.insert(0, files())
    events().serve(server)
    return server


class MCPEndpoint:
    """An ASGI endpoint serving the editor's tools and events, stateless."""

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
        # The manager is meant to live as long as the app, in a lifespan the
        # editor's many test apps never enter. A stateless request shares
        # nothing with the next, so each gets a manager of its own.
        manager = StreamableHTTPSessionManager(
            mcp_server(),
            json_response=True,
            stateless=True,
            security_settings=TRANSPORT_SECURITY,
            # The routes took a body of any size, and `mo.ui.file` sends a
            # file's contents inside a UI element's value.
            max_request_body_size=sys.maxsize,
        )
        # Made here, since the events endpoint hands tools a copy of the scope.
        tasks = scope.setdefault(_AFTER_REPLY, [])
        # Event hooks read the request, which the SDK does not hand them.
        token = request_var.set(request)
        try:
            async with manager.run():
                await events().asgi(manager)(scope, receive, send)
        finally:
            request_var.reset(token)
        for task in tasks:
            await task()


mcp_endpoint = MCPEndpoint()


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
