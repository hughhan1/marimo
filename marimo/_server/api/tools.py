# Copyright 2026 Marimo. All rights reserved.
"""The server's MCP endpoint at `/mcp`: its tools and events.

It serves the editor's tools, the Host Protocol's and the assistant's, and
the `session`, `catalog` and `notebook` events. A tool's arguments are the
fields of its request, its result is its reply, and a failure is a problem
carried as the error's text. The SDK owns the transport: the JSON-RPC
envelope, the mirrored headers, the protocol version and `server/discover`.
The arguments are decoded into the request's type by msgspec, which refuses
what the type does not declare. Files travel by the MCP files extension:
uploads and replies that are files go to and from `/mcp/files`, outside the
tool calls.
"""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from functools import cache, partial
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar, get_type_hints

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
from starlette.responses import JSONResponse, Response

from marimo import __version__
from marimo._ai._tools.base import ToolBase, ToolContext
from marimo._ai._tools.tools_registry import SUPPORTED_BACKEND_AND_MCP_TOOLS
from marimo._ai._tools.utils.exceptions import ToolExecutionError
from marimo._core import protocol
from marimo._core.operations import (
    PlainKind,
    is_problem,
    problem as make_problem,
)
from marimo._core.protocol_tools import TOOLS as HOST_TOOLS
from marimo._server.api.deps import AppState
from marimo._server.api.events import SessionEvents
from marimo._server.errors import handle_error

if TYPE_CHECKING:
    import os
    from collections.abc import Awaitable, Callable, Mapping

    from mcp.server.context import ServerRequestContext
    from starlette.applications import Starlette
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
NOTEBOOK_ID: Final = msgspec.json.schema(protocol.NotebookId)
NotebookUse = Literal["required", "optional"]


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
    notebook: NotebookUse | None = None


TOOLS: dict[str, Tool] = {}
F = TypeVar("F", bound="Callable[..., object]")


def tool(
    name: str, *, notebook: NotebookUse | None = None
) -> Callable[[F], F]:
    """Serve `handler` as the tool `name`.

    A tool that acts on a notebook takes its `notebook_id`, which `notebook`
    says is required or optional, and serves the call with its session.
    """

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
            notebook=notebook,
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


class _Notebook(msgspec.Struct):
    notebook_id: protocol.NotebookId


class _MaybeNotebook(msgspec.Struct):
    notebook_id: protocol.NotebookId | None = None


_NOTEBOOK: Final[dict[NotebookUse, type[_Notebook | _MaybeNotebook]]] = {
    "required": _Notebook,
    "optional": _MaybeNotebook,
}


def _notebook_id(
    found: Tool, arguments: dict[str, Any]
) -> protocol.NotebookId | None:
    """Take the notebook a call names out of its arguments."""
    if found.notebook is None:
        return None
    named = msgspec.convert(arguments, _NOTEBOOK[found.notebook])
    arguments.pop("notebook_id", None)
    return named.notebook_id


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
    and result's types as `input` and `output`, as `arguments` and
    `result`, as the host protocol's tools name them, or as `Args` and
    `Output`, as the assistant's tools do.
    """

    def parts(found: Any) -> tuple[Any, Any, str | None, NotebookUse | None]:
        if isinstance(found, Tool):
            return found.input, found.output, found.title, found.notebook
        if isinstance(found, ToolBase):
            return found.Args, found.Output, None, None
        return found.arguments, found.result, found.title, None

    types_ = list(
        dict.fromkeys(
            kind
            for found in tools.values()
            for kind in parts(found)[:2]
            if kind not in (None, FileResult)
        )
    )
    schemas_, components = msgspec.json.schema_components(
        types_, ref_template=SCHEMA_REF
    )
    schema_of = dict(zip(types_, schemas_, strict=True))
    definitions: list[dict[str, Any]] = []
    for name, found in sorted(tools.items()):
        given, reply, title, notebook = parts(found)
        if given is None:
            arguments: dict[str, Any] = {"type": "object", "properties": {}}
        else:
            arguments = inline_schema(given, schema_of, components)
            arguments.pop("title", None)
            arguments.pop("description", None)
        properties = arguments.get("properties", {})
        required = arguments.get("required", [])
        if notebook is not None:
            properties = {"notebook_id": NOTEBOOK_ID, **properties}
        if notebook == "required":
            required = ["notebook_id", *required]
        definition: dict[str, Any] = {
            "name": name,
            **({"title": title} if title else {}),
            "description": found.description,
            "inputSchema": {
                **arguments,
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
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


def assistant_tools(
    app: Starlette | None = None,
) -> dict[str, ToolBase[Any, Any]]:
    """The tools the editor's assistant calls, which models may call here."""
    context = ToolContext(app=app)
    tools = (kind(context) for kind in SUPPORTED_BACKEND_AND_MCP_TOOLS)
    return {tool.name: tool for tool in tools}


@cache
def contract() -> list[dict[str, Any]]:
    """The tool definitions.

    The editor's own tools are for its page; the Host Protocol's and the
    assistant's are for models too.
    """
    return sorted(
        [
            *(
                {**definition, "_meta": APP_ONLY}
                for definition in schemas(TOOLS)
            ),
            *schemas({**HOST_TOOLS, **assistant_tools()}),
        ],
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
    found = TOOLS.get(params.name)
    if found is None:
        from marimo._server.host.tools import call

        if params.name in HOST_TOOLS:
            return tool_result(
                params.name,
                await call(request, params.name, params.arguments or {}),
            )
        assistant = assistant_tools(request.app).get(params.name)
        if assistant is None:
            raise MCPError(
                code=INVALID_PARAMS, message=f"Unknown tool {params.name}"
            )
        return await _assistant_call(
            request, assistant, params.arguments or {}
        )
    arguments = dict(params.arguments or {})
    try:
        notebook_id = _notebook_id(found, arguments)
        given = _arguments(found, arguments)
        if notebook_id is not None:
            AppState(request).open_notebook(notebook_id)
        result = found.handler(request=request, **given)
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


async def _assistant_call(
    request: Request, tool: ToolBase[Any, Any], arguments: dict[str, Any]
) -> types.CallToolResult:
    if "edit" not in request.auth.scopes:
        return tool_result(
            tool.name,
            make_problem(
                "forbidden", "Only an editor may call the assistant's tools"
            ),
        )
    try:
        reply = msgspec.to_builtins(await tool(arguments))
    except ToolExecutionError as error:
        return types.CallToolResult(
            is_error=True,
            content=[
                types.TextContent(
                    type="text",
                    text=f"Error executing tool {tool.name}: {error}",
                )
            ],
        )
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(reply, indent=2, ensure_ascii=False),
            )
        ],
        structured_content=reply,
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


class MCPEndpoint:
    """The server's MCP endpoint: every tool and event, stateless."""

    def __init__(self) -> None:
        from marimo._server.host.streams import host_events

        self.storage = LocalStorage()
        definitions = {
            definition["name"]: definition for definition in contract()
        }
        files = FileServer(
            self.storage,
            tools=definitions.get,
            max_inline_bytes=_MAX_INLINE_BYTES,
        )

        listing = types.ListToolsResult(
            tools=[types.Tool.model_validate(each) for each in contract()]
        )

        async def list_tools(
            ctx: ServerRequestContext[Any, Any], params: Any
        ) -> types.ListToolsResult:
            del ctx, params
            return listing

        def input_schema(name: str) -> dict[str, Any] | None:
            found = definitions.get(name)
            return None if found is None else found["inputSchema"]

        self.server: Server[Any] = Server(
            "marimo",
            version=__version__,
            on_list_tools=list_tools,
            on_call_tool=partial(_call, files),
            get_tool_input_schema=input_schema,
        )
        self.server.extensions[EXTENSION] = files.settings()
        self.server.middleware.insert(0, files)
        self.session_events = SessionEvents()
        host_events(self.session_events.events)
        self.session_events.events.serve(self.server)

    def close(self) -> None:
        """End each event stream with its final frame.

        A client then opens the stream again from its cursor at once. Call it
        as the server starts to stop, on the loop that serves the endpoint.
        """
        self.session_events.events.close()

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
        if not request.auth.scopes:
            problem = make_problem(
                "unauthorized", "Send the server's access token"
            )
            unauthorized = Response(
                msgspec.json.encode(problem),
                status_code=problem.status,
                media_type="application/problem+json",
                headers={"WWW-Authenticate": "Bearer"},
            )
            await unauthorized(scope, receive, send)
            return
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
