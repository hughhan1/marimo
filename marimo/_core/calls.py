# Copyright 2026 Marimo. All rights reserved.
"""The host's operations, as the tool calls a client sends.

A call names the notebook, run or input it acts on in `params`, carries the
idempotency key and preconditions in `headers`, and the request itself in
`body`. On the wire these parts are flattened into one set of arguments, by
their Python names. Its `handle` method does the work and declares its reply
as its return type.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any, get_type_hints

import msgspec
import msgspec.structs

from marimo._core import protocol
from marimo._core.errors import Conflict, HostError, NotFound
from marimo._core.operations import Reply, problem_for

if TYPE_CHECKING:
    from marimo._core.backend import RequestInfo
    from marimo._core.operations import Mutations, NotebookBridge


class NotebookParams(msgspec.Struct, frozen=True):
    notebook_id: protocol.NotebookId


class RunParams(NotebookParams, frozen=True):
    run_id: protocol.RunId


class InputParams(NotebookParams, frozen=True):
    input_id: protocol.ItemId


class MutationHeaders(msgspec.Struct, frozen=True):
    idempotency_key: str = msgspec.field(name="idempotency-key")


class SaveHeaders(MutationHeaders, frozen=True):
    if_match: str | None = msgspec.field(name="if-match", default=None)


class _Call(msgspec.Struct, frozen=True, tag_field="operation"):
    """A call, tagged by the operation it names."""


class EditCall(_Call, frozen=True, tag="notebook.edit"):
    """Change parts of the document."""

    params: NotebookParams
    headers: MutationHeaders
    body: protocol.EditRequest

    async def handle(self, bridge: NotebookBridge) -> protocol.EditResponse:
        return await bridge.edit(
            self.params.notebook_id,
            self.body.changes,
            request_id=self.headers.idempotency_key,
        )


class SaveCall(_Call, frozen=True, tag="notebook.save"):
    """Write the document to the host's store."""

    params: NotebookParams
    headers: SaveHeaders
    body: protocol.SaveRequest

    async def handle(self, bridge: NotebookBridge) -> protocol.EditResponse:
        decisions = self.body.decisions
        return await bridge.save(
            self.params.notebook_id,
            if_match=_if_match(self.headers.if_match),
            decisions={} if decisions is msgspec.UNSET else decisions,
            request_id=self.headers.idempotency_key,
        )


class StartCall(_Call, frozen=True, tag="runtime.start"):
    """Start the notebook's runtime, or find the one it has."""

    params: NotebookParams
    headers: MutationHeaders
    body: protocol.StartRuntimeRequest

    async def handle(self, bridge: NotebookBridge) -> protocol.Runtime:
        notebook = await bridge.host.notebook(self.params.notebook_id)
        runtime, _ = notebook.start(request_id=self.headers.idempotency_key)
        return runtime


class StopCall(_Call, frozen=True, tag="runtime.stop"):
    """Stop the notebook's runtime."""

    params: NotebookParams
    headers: MutationHeaders

    async def handle(self, bridge: NotebookBridge) -> protocol.Runtime:
        notebook = await bridge.host.notebook(self.params.notebook_id)
        return notebook.stop(request_id=self.headers.idempotency_key)


class RestartCall(_Call, frozen=True, tag="runtime.restart"):
    """Restart the notebook's kernel."""

    params: NotebookParams
    headers: MutationHeaders

    async def handle(self, bridge: NotebookBridge) -> protocol.Runtime:
        notebook = await bridge.host.notebook(self.params.notebook_id)
        return notebook.restart(request_id=self.headers.idempotency_key)


class RunCall(_Call, frozen=True, tag="runtime.run"):
    """Run notebook cells by id under the runtime's execution settings."""

    params: NotebookParams
    headers: MutationHeaders
    body: protocol.RunRequest

    async def handle(
        self,
        bridge: NotebookBridge,
        request: RequestInfo | None,
        *,
        allow_input: bool = False,
    ) -> protocol.Run:
        notebook = await bridge.host.notebook(self.params.notebook_id)
        return notebook.run(
            cells=self.body.cells,
            request_id=self.headers.idempotency_key,
            request=request,
            allow_input=allow_input,
        )


class ExecuteCall(_Call, frozen=True, tag="runtime.execute"):
    """Execute Python with access to notebook values and the code-mode context."""

    params: NotebookParams
    headers: MutationHeaders
    body: protocol.ExecuteRequest

    async def handle(
        self,
        bridge: NotebookBridge,
        request: RequestInfo | None,
        *,
        allow_input: bool = False,
    ) -> protocol.Run:
        notebook = await bridge.host.notebook(self.params.notebook_id)
        return notebook.run(
            self.body.code,
            request_id=self.headers.idempotency_key,
            request=request,
            allow_input=allow_input,
        )


class InterruptCall(_Call, frozen=True, tag="runtime.interrupt"):
    """Interrupt the notebook's runtime."""

    params: NotebookParams
    headers: MutationHeaders

    async def handle(self, bridge: NotebookBridge) -> protocol.Runtime:
        notebook = await bridge.host.notebook(self.params.notebook_id)
        return notebook.interrupt(request_id=self.headers.idempotency_key)


class CancelCall(_Call, frozen=True, tag="runtime.cancel"):
    """Cancel a run."""

    params: RunParams
    headers: MutationHeaders

    async def handle(self, bridge: NotebookBridge) -> protocol.Run:
        notebook_id, run_id = self.params.notebook_id, self.params.run_id
        notebook = await bridge.host.notebook(notebook_id)
        try:
            return notebook.cancel(
                run_id, request_id=self.headers.idempotency_key
            )
        except NotFound:
            run = bridge.host.run(notebook_id, run_id)
            if run is not None and run.status == "completed":
                raise Conflict(f"run {run_id} has already ended.") from None
            raise


class InputCall(_Call, frozen=True, tag="runtime.input"):
    """Answer a pending input."""

    params: InputParams
    headers: MutationHeaders
    body: protocol.InputReplyRequest

    async def handle(self, bridge: NotebookBridge) -> protocol.Input:
        notebook_id, input_id = self.params.notebook_id, self.params.input_id
        notebook = await bridge.host.notebook(notebook_id)
        try:
            return notebook.reply_input(
                input_id,
                self.body.value,
                request_id=self.headers.idempotency_key,
            )
        except NotFound:
            if bridge.host.input(notebook_id, input_id) is not None:
                raise Conflict(
                    f"input {input_id} has already ended."
                ) from None
            raise


OperationCall = (
    EditCall
    | SaveCall
    | StartCall
    | StopCall
    | RestartCall
    | RunCall
    | ExecuteCall
    | InterruptCall
    | CancelCall
    | InputCall
)
"""A request for any notebook operation, tagged by its operation id."""

# The same members as `OperationCall`, as values to build tools from.
# `typing.get_args` would return them untyped.
CALLS: tuple[type[OperationCall], ...] = (
    EditCall,
    SaveCall,
    StartCall,
    StopCall,
    RestartCall,
    RunCall,
    ExecuteCall,
    InterruptCall,
    CancelCall,
    InputCall,
)


class CreateNotebookCall(msgspec.Struct, frozen=True):
    """Create an empty notebook in the project."""

    headers: MutationHeaders
    body: protocol.CreateNotebookRequest


class UpdateNotebookCall(msgspec.Struct, frozen=True):
    """Change a notebook's path or display name, preserving its runtime."""

    params: NotebookParams
    headers: MutationHeaders
    body: protocol.UpdateNotebookRequest


class DeleteNotebookCall(msgspec.Struct, frozen=True):
    """Delete the notebook and its file if it has no runtime."""

    params: NotebookParams
    headers: MutationHeaders


class OpenNotebookCall(msgspec.Struct, frozen=True):
    """Open the notebook in the configured browser."""

    params: NotebookParams
    headers: MutationHeaders


BY_OPERATION: dict[str, type[OperationCall]] = {
    str(kind.__struct_config__.tag): kind for kind in CALLS
}
"""Each notebook operation's call, by its operation id."""


@cache
def arguments_of(call: type[msgspec.Struct]) -> type[msgspec.Struct]:
    """A struct whose fields are those of the call's parts, by Python name."""
    members: list[Any] = []
    for part in msgspec.structs.fields(call):
        for member in msgspec.structs.fields(part.type):
            if member.default is not msgspec.NODEFAULT:
                members.append((member.name, member.type, member.default))
            elif member.default_factory is not msgspec.NODEFAULT:
                members.append(
                    (
                        member.name,
                        member.type,
                        msgspec.field(default_factory=member.default_factory),
                    )
                )
            else:
                members.append((member.name, member.type))
    return msgspec.defstruct(
        f"{call.__name__}Arguments",
        members,
        kw_only=True,
        frozen=True,
        forbid_unknown_fields=True,
        module=call.__module__,
    )


def assemble(call: type[msgspec.Struct], arguments: dict[str, Any]) -> Any:
    """The call that flat `arguments` describe.

    Raises `msgspec.ValidationError` when they do not fit the call.
    """
    flat = msgspec.convert(arguments, arguments_of(call))
    parts: dict[str, Any] = {}
    config = call.__struct_config__
    if config.tag is not None and config.tag_field is not None:
        parts[config.tag_field] = config.tag
    for part in msgspec.structs.fields(call):
        parts[part.name] = {
            member.encode_name: getattr(flat, member.name)
            for member in msgspec.structs.fields(part.type)
            if getattr(flat, member.name) is not msgspec.UNSET
        }
    return msgspec.convert(parts, call, from_attributes=True)


def reply_type(kind: type[OperationCall]) -> Any:
    """The type of the reply a call's `handle` declares."""
    return get_type_hints(
        kind.handle,
        localns={"NotebookBridge": object, "RequestInfo": object},
    )["return"]


async def dispatch(
    mutations: Mutations,
    bridge: NotebookBridge,
    call: OperationCall,
    *,
    request: RequestInfo | None,
    allow_input: bool = False,
) -> Reply:
    """Serve a call once per idempotency key and describe a failure as a problem.

    `request` is the HTTP request carrying the call, if any; runs hand it
    to the code they execute.
    """

    async def serve() -> Reply:
        if isinstance(call, (RunCall, ExecuteCall)):
            return await call.handle(bridge, request, allow_input=allow_input)
        return await call.handle(bridge)

    try:
        return await mutations.execute(
            str(call.__struct_config__.tag),
            notebook_id=call.params.notebook_id,
            key=call.headers.idempotency_key,
            payload=call,
            call=serve,
        )
    except HostError as e:
        return problem_for(e)


def _if_match(header: str | None) -> tuple[str, ...] | None:
    """Parse strong entity tags; weak tags never satisfy an If-Match check."""
    if header is None or header.strip() == "*":
        return None
    return tuple(
        part.strip().strip('"')
        for part in header.split(",")
        if part.strip() and not part.strip().startswith("W/")
    )
