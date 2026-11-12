# Copyright 2026 Marimo. All rights reserved.
# AUTO-GENERATED FILE — DO NOT EDIT.
# Generated from packages/host-protocol.
# Regenerate with `make host-protocol-codegen`.
"""The host protocol's tools, by name."""

from __future__ import annotations

from typing import Any, NamedTuple

from marimo._core import protocol


class Tool(NamedTuple):
    title: str
    description: str
    arguments: type[Any]
    result: type[Any] | None


TOOLS: dict[str, Tool] = {
    "notebook.create": Tool(
        "Create a notebook",
        "Adds an empty notebook to the project. The call carries the\nnotebook's own fields, and the host fills in what it owns. In a\nproject with a `root`, `path` is required and `name` defaults to\nthe filename; if a file already exists at `path`, the host\nanswers `conflict` and leaves it alone. In a project without a\nroot, `name` is required, the host chooses storage, and `path` is\nnull. A call missing the required field is `invalid-request`.\nThe new notebook appears on the catalog stream with this\ncall's key.\n",
        protocol.CreateNotebookRequest,
        protocol.Notebook,
    ),
    "notebook.delete": Tool(
        "Delete a notebook",
        "Deletes the file. While the notebook has a runtime, the host\nanswers `conflict` instead.\n",
        protocol.NotebookRequest,
        None,
    ),
    "notebook.edit": Tool(
        "Change parts of the document",
        "Applies an ordered list of changes to the document as one unit.\nEach change says what it replaces and carries the etag that state\nhad when the client read it, so the host can detect competing\nedits to that state. Every etag is compared with the document as\nit was when the request arrived, so one request can create a cell\nand then place it with the order etag the client read. Cells and\nthe order each have their own etag, so writers can update different\nparts independently. An etag of `*` skips the check. A `put_cell`\nwith a null etag creates the cell, after or before an existing cell\nor at the end; placement has no precondition, so two clients\ninserting at once both succeed.\n\nIf any precondition is stale, nothing is applied and the host\nanswers `precondition-failed`. Its `index` names the first stale\nchange, and it carries the cell and the document as they now\nare. A change that could not apply to any state, such as an\norder that leaves out a cell or names one twice, is\n`invalid-request`.\n\nOn success the document has changed, and every attachment sees\nthe effects on the notebook's stream with this call's key: the\ncells that changed, `cell.removed` for each deleted cell, and then\nthe document, with nothing in between. The result carries the new\netags, as of this edit or a later revision, so a client keeps editing without reading again. A\nchange that changes nothing keeps its etags and sends no events.\nWhether the result has reached the store shows in the document's\n`saved_etag`.\n",
        protocol.EditRequest,
        protocol.EditResponse,
    ),
    "notebook.open": Tool(
        "Show a notebook in the host's own interface",
        "Opens the notebook where the host normally shows notebooks, such\nas a browser tab or an editor, and returns the URL it used.\nOpening may start a runtime. The host omits its token from the URL,\nand a client opens it exactly as given. If the notebook\ncannot be shown, for example because its file is gone, the host\nanswers `conflict`.\n",
        protocol.NotebookRequest,
        protocol.OpenNotebookResponse,
    ),
    "notebook.save": Tool(
        "Write the document to the host's store",
        "Writes the document as currently applied, after which `saved_etag`\nequals `etag`. The call names only the notebook, because the host\nalready holds the state. A host that persists every change as\nit applies it need not offer this operation. A client shows a save control\nwhen the notebook lists it, and reads whether anything is unsaved\nfrom the two etags either way.\n\nThe result is an edit response, because a host may rewrite code\nas it saves, for example to format it. Such changes reach the\nstream like any other and the result carries the new etags.\n`expected_etag` makes the document etag the client last read a\nprecondition, and the host answers `precondition-failed` when the\ndocument has moved on. If the store has changed since the host\nlast read or wrote it, the host answers `decision-required`,\noffering to overwrite the store or to reload from it, and the\nclient calls again with its decision and a new key.\n",
        protocol.SaveRequest,
        protocol.EditResponse,
    ),
    "notebook.update": Tool(
        "Rename or move a notebook",
        "Changes `path`, `name`, or both. A new `path` moves the file\nunder the same project root, and an attached runtime follows it.\nA new `name` changes the label shown to people. What the file\ncontains is content, which this protocol does not write. If a\nfile already exists at `path`, the host answers `conflict`.\n",
        protocol.UpdateNotebookRequest,
        protocol.Notebook,
    ),
    "runtime.cancel": Tool(
        "Cancel a run",
        "Cancels one queued or running run, leaving other runs untouched.\nA queued run is removed from the queue and completes interrupted\nwithout executing. For a running run, the host interrupts only if\nthat run is still active; its remaining executions end interrupted.\nThe host must not interrupt a successor if the target finishes before\nthe cancellation takes effect. A completed run receives `conflict`.\nPending inputs belonging to cancelled work are cancelled.\n\nThe result acknowledges the call, not completion. The notebook\nstream carries the run and execution outcomes once they settle.\nThe runtime stays alive. A host that lists `runtime.run` or\n`runtime.execute` supports this operation.\n",
        protocol.CancelRunRequest,
        protocol.Run,
    ),
    "runtime.execute": Tool(
        "Execute Python with notebook values",
        "Executes Python with access to the notebook's current values without\nadding a cell. New variables and assignments stay within this call.\nObjects are shared with the notebook, so changing a list or dataframe\ncan affect it.\n\nOn marimo hosts, the internal `marimo._code_mode.get_context()` API\nprovides access to notebook cells and captured outputs, along with\nmethods for editing and running cells. The outputs are captured when\nexecution begins.\n\nReturns a `Run` with status `queued` or `running`. Scratch executions\nand cell runs share one queue in acceptance order. Open the notebook\nstream before submitting work to receive its progress and results.\nThe execution includes the submitted code. Run and execution events\ncarry the call's `idempotency_key` as `request_id`.\n\nA notebook without a runtime returns `no-runtime`. Within the host's\nretry window, repeating the call returns its latest run record\nwithout executing again, even if its outputs have expired.\n",
        protocol.ExecuteRequest,
        protocol.Run,
    ),
    "runtime.input": Tool(
        "Answer an execution's pending input",
        "Accepts an answer for delivery to the identified input and returns\nidentity, metadata, and updated status `answered`. Any client\npermitted this operation may answer, regardless of who started\nthe run. The first accepted reply determines the answer.\n\nThe host returns `conflict` for a new call to an answered or\ncancelled input, or `not-found` if the input is unknown, removed,\nor belongs to another notebook. Retrying an accepted call with\nthe same key within the retry window returns the original result,\neven after the execution is removed, under the protocol's\nidempotency rules.\n\nHosts offering `runtime.run` or `runtime.execute` implement this\noperation. See [Answering input](spec.md#answering-input) for delivery semantics.\n",
        protocol.InputReplyRequest,
        protocol.Input,
    ),
    "runtime.interrupt": Tool(
        "Interrupt the notebook's runtime",
        "Requests interruption of active work in the notebook's current runtime,\nincluding work with no run or execution record. Queued runs complete\ninterrupted without executing. The runtime stays alive. Interruption\ndoes not roll back work already performed. Pending inputs belonging\nto interrupted work are cancelled.\n\nIf the notebook has no runtime, the host answers `no-runtime`. If its\nruntime is not `running`, the host answers `conflict`. An idle running\nruntime accepts the request as a no-op and does not arm an interrupt\nfor future work.\n\nThe result is the current runtime and acknowledges the request,\nnot that the work has stopped. Runs and executions report their actual\noutcomes on the stream once they settle; acceptance alone does not\nmark them interrupted. Work without a run record need not acquire one.\nWithin the retry window, repeating the same call returns the original\nacknowledgement without interrupting again, including after new work\nstarts or the runtime is replaced. Hosts offering `runtime.run` or\n`runtime.execute` support this operation.\n",
        protocol.NotebookRequest,
        protocol.Runtime,
    ),
    "runtime.restart": Tool(
        "Restart the notebook's kernel",
        "Stops the runtime and starts a new one with a new id. The stream\nshows the old runtime `terminating`, then `runtime.removed`, then\nthe new runtime `starting`, keeping one runtime attached to the\nnotebook at a time. Active and queued runs and their unfinished\nexecutions complete with outcome `interrupted`. Retained\nresults stay and are no longer live. The result is the replacement\nruntime in its `starting` state. The client follows that id until\nit is `running` or has `failed`. If the notebook has no runtime,\nthe host answers `no-runtime`.\n",
        protocol.NotebookRequest,
        protocol.Runtime,
    ),
    "runtime.run": Tool(
        "Run notebook cells",
        "Runs the cells named in `cells` under the notebook's execution settings,\nwhich determine whether dependent cells run too. Each execution names\nits cell through `cell_id`.\n\nInclude a cell's `etag` to run it only if it still matches what the\nclient read. A missing or changed cell is `precondition-failed` and\nnothing runs. The problem's `index` is the position of that cell\nin `cells`.\n\nReturns a `Run` with status `queued` or `running`. Cell runs and scratch\nexecutions share one queue in acceptance order. Open the notebook\nstream before submitting work to receive its progress and results.\nRun and execution events carry the call's `idempotency_key` as\n`request_id`.\n\nA notebook without a runtime returns `no-runtime`. Within the host's\nretry window, repeating the call returns its latest run record\nwithout executing again, even if its outputs have expired.\n",
        protocol.RunRequest,
        protocol.Run,
    ),
    "runtime.start": Tool(
        "Start the notebook's runtime, or find the one it has",
        "Makes sure the notebook has a running runtime. If it has none, the\nhost starts one and returns the runtime `starting`. The client then\nfollows `runtime` events for that runtime, on either\nstream, until its status is `running` or `failed`. If the notebook\nalready has a runtime, the host returns it and starts nothing.\n\nSometimes the host needs a choice the client did not make, such as\nwhich environment the kernel should run in. It then answers with\na `decision-required` problem that lists the options, and the\nclient calls again with a new `idempotency_key` and `decisions`\nfilled in. A client that already knows its choice\ncan include `decisions` the first time.\n",
        protocol.StartRuntimeRequest,
        protocol.Runtime,
    ),
    "runtime.stop": Tool(
        "Stop the notebook's runtime",
        "Ends the runtime for everyone attached to the notebook. A runtime\nthat is still starting is cancelled; a running kernel is shut\ndown. Active and queued runs and their unfinished executions complete\nwith outcome `interrupted`. Retained results stay\nand are no longer live. The notebook and its attachments stay,\nbut Python state is lost. The client follows the\nruntime on a stream until `runtime.removed`. If the notebook has\nno runtime, the host answers `no-runtime`.\n",
        protocol.NotebookRequest,
        protocol.Runtime,
    ),
}
