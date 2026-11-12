# Copyright 2026 Marimo. All rights reserved.
"""The host's notebook tools: one handler per tool, by the tool's name.

A handler takes the bridge, the tool's arguments decoded into the request
type the protocol generates, and who is calling, and returns the tool's
result. The catalog's tools, which only a host serves, live with the host.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import msgspec

from marimo._core import protocol
from marimo._core.errors import Conflict, HostError, NotFound
from marimo._core.operations import Reply, problem, problem_for
from marimo._core.protocol_tools import TOOLS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from marimo._core.backend import RequestInfo
    from marimo._core.operations import Mutations, NotebookBridge

    Handler = Callable[[NotebookBridge, Any, "Caller"], Awaitable[Reply]]


@dataclass(frozen=True)
class Caller:
    request: RequestInfo | None = None
    """The request that carried the call, which runs hand to their code."""
    allow_input: bool = False
    """Whether the caller's own session may answer the run's input."""


HANDLERS: dict[str, Handler] = {}
"""Each notebook tool's handler, by the tool's name."""


def _handles(name: str) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        HANDLERS[name] = handler
        return handler

    return register


@_handles("notebook.edit")
async def _edit(
    bridge: NotebookBridge, call: protocol.EditRequest, _: Caller
) -> protocol.EditResponse:
    return await bridge.edit(
        call.notebook_id, call.changes, request_id=call.idempotency_key
    )


@_handles("notebook.save")
async def _save(
    bridge: NotebookBridge, call: protocol.SaveRequest, _: Caller
) -> protocol.EditResponse:
    return await bridge.save(
        call.notebook_id,
        if_match=None
        if call.expected_etag is msgspec.UNSET
        else (call.expected_etag,),
        decisions={} if call.decisions is msgspec.UNSET else call.decisions,
        request_id=call.idempotency_key,
    )


@_handles("runtime.start")
async def _start(
    bridge: NotebookBridge, call: protocol.StartRuntimeRequest, _: Caller
) -> protocol.Runtime:
    notebook = await bridge.host.notebook(call.notebook_id)
    runtime, _started = notebook.start(request_id=call.idempotency_key)
    return runtime


@_handles("runtime.stop")
async def _stop(
    bridge: NotebookBridge, call: protocol.NotebookRequest, _: Caller
) -> protocol.Runtime:
    notebook = await bridge.host.notebook(call.notebook_id)
    return notebook.stop(request_id=call.idempotency_key)


@_handles("runtime.restart")
async def _restart(
    bridge: NotebookBridge, call: protocol.NotebookRequest, _: Caller
) -> protocol.Runtime:
    notebook = await bridge.host.notebook(call.notebook_id)
    return notebook.restart(request_id=call.idempotency_key)


@_handles("runtime.run")
async def _run(
    bridge: NotebookBridge, call: protocol.RunRequest, caller: Caller
) -> protocol.Run:
    notebook = await bridge.host.notebook(call.notebook_id)
    return notebook.run(
        cells=call.cells,
        request_id=call.idempotency_key,
        request=caller.request,
        allow_input=caller.allow_input,
    )


@_handles("runtime.execute")
async def _execute(
    bridge: NotebookBridge, call: protocol.ExecuteRequest, caller: Caller
) -> protocol.Run:
    notebook = await bridge.host.notebook(call.notebook_id)
    return notebook.run(
        call.code,
        request_id=call.idempotency_key,
        request=caller.request,
        allow_input=caller.allow_input,
    )


@_handles("runtime.interrupt")
async def _interrupt(
    bridge: NotebookBridge, call: protocol.NotebookRequest, _: Caller
) -> protocol.Runtime:
    notebook = await bridge.host.notebook(call.notebook_id)
    return notebook.interrupt(request_id=call.idempotency_key)


@_handles("runtime.cancel")
async def _cancel(
    bridge: NotebookBridge, call: protocol.CancelRunRequest, _: Caller
) -> protocol.Run:
    notebook = await bridge.host.notebook(call.notebook_id)
    try:
        return notebook.cancel(call.run_id, request_id=call.idempotency_key)
    except NotFound:
        run = bridge.host.run(call.notebook_id, call.run_id)
        if run is not None and run.status == "completed":
            raise Conflict(f"run {call.run_id} has already ended.") from None
        raise


@_handles("runtime.input")
async def _input(
    bridge: NotebookBridge, call: protocol.InputReplyRequest, _: Caller
) -> protocol.Input:
    notebook = await bridge.host.notebook(call.notebook_id)
    try:
        return notebook.reply_input(
            call.input_id, call.value, request_id=call.idempotency_key
        )
    except NotFound:
        if bridge.host.input(call.notebook_id, call.input_id) is not None:
            raise Conflict(
                f"input {call.input_id} has already ended."
            ) from None
        raise


def decode(name: str, arguments: object) -> Any:
    """The tool's arguments as its request type.

    Raises `msgspec.ValidationError` when the tool is unknown or the
    arguments do not fit it.
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise msgspec.ValidationError(f"No tool {name}")
    return msgspec.convert(arguments, tool.arguments)


async def once(
    mutations: Mutations,
    name: str,
    call: Any,
    serve: Callable[[], Awaitable[Reply]],
) -> Reply:
    """Serve a call once per idempotency key, and a failure as its problem."""
    try:
        return await mutations.execute(
            name,
            notebook_id=getattr(call, "notebook_id", None),
            key=call.idempotency_key,
            payload=call,
            call=serve,
        )
    except HostError as e:
        return problem_for(e)


async def dispatch(
    mutations: Mutations,
    bridge: NotebookBridge,
    name: str,
    call: Any,
    caller: Caller,
) -> Reply:
    """Serve a notebook tool's decoded call."""
    handler = HANDLERS.get(name)
    if handler is None:
        return problem("invalid-request", f"No notebook tool {name}")
    return await once(
        mutations, name, call, lambda: handler(bridge, call, caller)
    )
