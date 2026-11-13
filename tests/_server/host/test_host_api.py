# Copyright 2026 Marimo. All rights reserved.
"""Host API authorization, events, and commands."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import msgspec
import pytest
from starlette.testclient import TestClient

from marimo._core import backend as be, protocol
from marimo._core.host import Host
from marimo._core.notebook import Notebook
from marimo._core.operations import problem
from marimo._core.protocol import ProjectMessage
from marimo._core.run_log import RetentionPolicy
from marimo._core.stream import RoutedEvent, event_name
from marimo._messaging.notification import CompletedRunNotification
from marimo._server.host import HOST_API_PATH, api as api_module, streams
from marimo._server.host.bridge import SessionBridge
from marimo._server.host.context import HostContext
from marimo._server.host.events import notification_event
from marimo._server.main import create_starlette_app
from marimo._server.models.home import MarimoFile
from marimo._server.workspace import FixedFilesWorkspace, SingleFileWorkspace
from marimo._session.notebook import AppFileManager
from tests._core.conftest import ScriptedBackend, make_notebook, runtime_id_of
from tests._server.host.drive import (
    AUTH,
    AUTHORITY,
    LOOPBACK,
    PORT,
    TOKEN,
    Response,
    call,
    finished,
    position,
    refused,
    subscribe,
    until_ready,
    wait_for,
    with_client_address,
)
from tests._server.mcp_client import call_tool
from tests._server.mocks import (
    get_mock_session_manager,
    get_starlette_server_state_init,
)
from tests.mcp_request import Called, stream_headers, stream_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.applications import Starlette

    from marimo._core.stream import Sink


async def analyzed(notebook: Notebook) -> protocol.Analysis:
    """Wait for the notebook's analysis, which runs off the event loop."""
    deadline = asyncio.get_running_loop().time() + 5
    while notebook.analysis is None:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("the notebook was not analyzed")
        await asyncio.sleep(0.01)
    return notebook.analysis


class Harness:
    def __init__(
        self, app: Starlette, bridge: SessionBridge, root: Path
    ) -> None:
        self.app = app
        self.bridge = bridge
        self.root = root
        self.now = 0.0
        self.host.clock = lambda: self.now
        self.backends: dict[str, ScriptedBackend] = {}

    @property
    def host(self) -> Host:
        return self.bridge.host

    @property
    def notebook_id(self) -> str:
        (notebook,) = [
            n for n in self.host.notebooks.values() if n.path == "forecasts.py"
        ]
        return notebook.id

    def backend(self) -> ScriptedBackend:
        return self.backends[self.notebook_id]

    async def running(self) -> str:
        """Start a runtime and simulate the backend's running update."""
        started = await call(
            self.app,
            "runtime.start",
            idempotency_key="start",
            notebook_id=self.notebook_id,
        )
        assert started.ok, started.problem
        (await self.host.notebook(self.notebook_id)).observe(
            be.Running(
                runtime_id_of(await self.host.notebook(self.notebook_id)),
                "0.25.0",
            )
        )
        return self.notebook_id


@pytest.fixture
def harness(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    (tmp_path / "forecasts.py").write_text(
        "import marimo\napp = marimo.App()\n"
    )
    workspace = FixedFilesWorkspace(
        [MarimoFile(name="forecasts.py", path=str(tmp_path / "forecasts.py"))],
        directory=str(tmp_path),
    )
    app = create_starlette_app(base_url="", enable_auth=True)
    manager = get_mock_session_manager()
    manager.workspace = workspace
    get_starlette_server_state_init(session_manager=manager).apply(app.state)
    bridge = SessionBridge(manager)
    bridge.host.retention = getattr(request, "param", RetentionPolicy())
    harness = Harness(app, bridge, tmp_path)

    def make(notebook_id: str, sink: Sink) -> ScriptedBackend:
        del sink
        backend = ScriptedBackend()
        harness.backends[notebook_id] = backend
        return backend

    monkeypatch.setattr(bridge, "_make_backend", make)
    bridge.index.refresh()
    context = HostContext(bridge=bridge, token=TOKEN, port=PORT)
    context.mutations.receipt_seconds = 30
    context.mutations.clock = lambda: harness.now
    app.state.host_context = context
    return harness


async def test_the_token_the_address_and_the_name_are_all_checked(
    harness: Harness,
) -> None:
    app = harness.app

    async def status(**kwargs: Any) -> int:
        return int((await refused(app, "catalog", **kwargs))["status"])

    assert await status(headers={}) == 401
    assert await status(headers={"Authorization": "Bearer nope"}) == 401
    assert await status(client=("10.0.0.5", 1)) == 403
    assert await status(host="evil.example:1234") == 403
    assert await status(host="127.0.0.1:9999") == 403
    app.state.host_context = None
    problem = await refused(app, "catalog")
    assert problem["status"] == 404
    assert problem["type"].endswith(":not-found")


async def test_unknown_routes_are_problems(harness: Harness) -> None:
    response = await finished(harness.app, "GET", "/nothing", headers=AUTH)
    assert response.status == 404
    assert response.headers["content-type"] == "application/problem+json"


async def test_a_wrong_method_on_a_known_route_is_a_405_problem(
    harness: Harness,
) -> None:
    response = await finished(harness.app, "GET", "/mcp", headers=AUTH)
    assert response.status == 405
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith(":method-not-allowed")
    assert "POST" in response.headers["allow"]


@pytest.mark.parametrize(
    ("host", "admitted"),
    [
        ("127.attacker.example:1234", False),
        ("127.1.2.3:1234", True),
        ("localhost:1234", True),
        ("LOCALHOST:1234", True),
        ("[::1]:1234", True),
        ("[::ffff:127.0.0.1]:1234", True),
        ("[::1]junk:1234", False),
        ("[::1", False),
        ("[::1:1234", False),
        ("127.0.0.1:" + "1" * 5000, False),
    ],
)
async def test_only_loopback_names_are_admitted(
    harness: Harness, host: str, admitted: bool
) -> None:
    started = await call(
        harness.app,
        "runtime.start",
        host=host,
        idempotency_key="start",
        notebook_id=harness.notebook_id,
    )
    assert started.ok is admitted
    if not admitted:
        assert started.problem["status"] == 403


@pytest.mark.parametrize(
    "port",
    ["\xb2", "1234\xb2", "\xb9\xb2\xb3"],
)
def test_latin1_superscripts_are_not_a_port(port: str) -> None:
    from marimo._server.host.auth import host_header_ok

    assert port.isdigit()
    assert host_header_ok(f"127.0.0.1:{port}", PORT) is False


@pytest.mark.parametrize(
    ("authorization", "kind"),
    [
        ("Bearer \xe9", "unauthorized"),
        ("Bearer", "unauthorized"),
        ("Bearer ", "unauthorized"),
        (f"Basic {TOKEN}", "unauthorized"),
        # An empty update reaches its tool only with the host's bearer.
        (f"bearer  {TOKEN} ", "invalid-request"),
    ],
)
async def test_malformed_bearers_are_401_problems(
    harness: Harness, authorization: str, kind: str
) -> None:
    # The notebook record answers only the host's bearer, never the editor.
    updated = await call(
        harness.app,
        "notebook.update",
        headers={"Authorization": authorization},
        idempotency_key="update",
        notebook_id=harness.notebook_id,
    )
    assert updated.problem["type"].endswith(f":{kind}")


async def test_an_unknown_cursor_resets_a_filtered_stream(
    harness: Harness,
) -> None:
    response, task, disconnect = subscribe(
        harness.app, "catalog", cursor="previous-host-1", types=["notebook"]
    )
    await wait_for(response, until_ready)
    assert response.names() == ["reset", "notebook", "ready"]
    assert response.records[0] == {"event": "reset", "data": {}}
    assert response.records[-1]["data"] == {}
    assert isinstance(response.records[-1]["id"], str)
    disconnect()
    await asyncio.wait_for(task, 5)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("catalog", {"cursor": ""}),
        ("notebook", {"cursor": ""}),
        ("notebook", {"run_id": ""}),
    ],
)
async def test_an_empty_cursor_or_run_is_a_400_problem(
    harness: Harness, name: str, arguments: dict[str, Any]
) -> None:
    if name == "notebook":
        arguments = {**arguments, "notebook_id": harness.notebook_id}
    problem = await refused(harness.app, name, **arguments)
    assert problem["status"] == 400
    assert problem["type"].endswith(":invalid-request")


async def test_a_nul_byte_in_a_path_is_a_400_problem(harness: Harness) -> None:
    created = await call(
        harness.app,
        "notebook.create",
        idempotency_key="nul",
        project_id=harness.bridge.index.project_id,
        path="a\u0000b.py",
    )
    assert created.problem["type"].endswith(":invalid-request")


async def test_a_401_names_the_bearer_scheme(harness: Harness) -> None:
    response = await finished(harness.app, "POST", "/mcp")
    assert response.status == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("name", ["catalog", "notebook"])
@pytest.mark.parametrize(
    "types", [["bogus"], ["notebook", "bogus"], ["Notebook"]]
)
async def test_an_unknown_type_filter_is_a_400_problem(
    harness: Harness, name: str, types: list[str]
) -> None:
    arguments = (
        {"notebook_id": harness.notebook_id} if name == "notebook" else {}
    )
    notebook = await harness.host.notebook(harness.notebook_id)
    problem = await refused(harness.app, name, types=types, **arguments)
    assert problem["status"] == 400
    assert "bogus" in problem["detail"] or types == ["Notebook"]
    assert notebook._attachments == {}


async def test_an_unknown_argument_is_refused_before_subscribing(
    harness: Harness,
) -> None:
    error = await refused(
        harness.app, "notebook", notebook_id=harness.notebook_id, future=True
    )
    assert error["code"] == -32602
    assert harness.host.peek_notebook(harness.notebook_id) is None


async def test_an_unknown_request_member_is_a_400_problem(
    harness: Harness,
) -> None:
    updated = await call(
        harness.app,
        "notebook.update",
        idempotency_key="future",
        notebook_id=harness.notebook_id,
        name="Forecasts",
        future=True,
    )
    assert updated.problem["type"].endswith(":invalid-request")


async def test_a_call_of_any_size_reaches_its_operation(
    harness: Harness,
) -> None:
    # Larger than the MCP SDK's default cap, as a file in an input's value is.
    updated = await call(
        harness.app,
        "notebook.update",
        idempotency_key="large",
        notebook_id=harness.notebook_id,
        future="x" * (5 * 1024 * 1024),
    )
    assert updated.problem["type"].endswith(":invalid-request")


async def test_an_empty_update_is_a_400_problem(harness: Harness) -> None:
    updated = await call(
        harness.app,
        "notebook.update",
        idempotency_key="empty",
        notebook_id=harness.notebook_id,
    )
    assert updated.problem["type"].endswith(":invalid-request")


async def test_a_failure_while_delivering_ends_the_subscription(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook = await harness.host.notebook(harness.notebook_id)
    listeners = len(harness.host._stream._listeners)
    response, task, _ = subscribe(
        harness.app, "notebook", notebook_id=notebook.id, types=["custom"]
    )
    await wait_for(response, until_ready)

    def broken(_event: object) -> str:
        raise RuntimeError("a secret detail")

    monkeypatch.setattr(streams, "serialize_event", broken)
    notebook.emit("test.unsent", {})
    await asyncio.wait_for(task, 5)
    problem = response.ended["data"]["problem"]
    assert problem["type"].endswith(":internal")
    assert "secret" not in str(response.ended)
    assert response.names() == ["ready"]
    assert notebook._attachments == {}
    assert len(harness.host._stream._listeners) == listeners


async def test_the_catalog_stream_sends_a_snapshot_then_changes(
    harness: Harness,
) -> None:
    response, _, disconnect = subscribe(harness.app, "catalog")
    await wait_for(response, until_ready)
    assert response.names() == ["host", "project", "notebook", "ready"]
    assert "id" not in response.records[0]
    cursor = response.records[-1]["id"]

    notebook = await harness.host.notebook(harness.notebook_id)
    notebook.start()
    await wait_for(response, lambda r: r.names()[-1] == "runtime")
    live = response.records[-1]
    assert position(harness.host, live["id"]) > position(harness.host, cursor)
    assert live["data"]["runtime"]["status"] == "starting"
    assert "time" in live["data"]
    disconnect()


async def test_catalog_refresh_delivers_updates_on_the_event_loop(
    harness: Harness,
) -> None:
    first, first_task, disconnect_first = subscribe(harness.app, "catalog")
    await wait_for(first, until_ready)
    threads: list[int] = []
    unsubscribe = harness.host.subscribe(
        lambda _: threads.append(threading.get_ident())
    )
    new_file = harness.root / "new.py"
    new_file.write_text("import marimo\n")
    harness.bridge.index._workspace = FixedFilesWorkspace(
        [
            MarimoFile(
                name="forecasts.py", path=str(harness.root / "forecasts.py")
            ),
            MarimoFile(name="new.py", path=str(new_file)),
        ],
        directory=str(harness.root),
    )
    second, second_task, disconnect_second = subscribe(harness.app, "catalog")
    try:
        await wait_for(second, until_ready)
        await wait_for(
            first,
            lambda response: any(
                r.get("data", {}).get("notebook", {}).get("path") == "new.py"
                for r in response.records
            ),
        )
        assert threads
        assert set(threads) == {threading.get_ident()}
    finally:
        unsubscribe()
        disconnect_first()
        disconnect_second()
        await asyncio.gather(first_task, second_task)


async def test_types_and_cursors_apply_to_the_stream(harness: Harness) -> None:
    first, _, disconnect = subscribe(
        harness.app, "catalog", types=["notebook"]
    )
    await wait_for(first, until_ready)
    assert first.names() == ["notebook", "ready"]
    cursor = first.records[-1]["id"]
    disconnect()

    (await harness.host.notebook(harness.notebook_id)).start()
    resumed, _, disconnect = subscribe(harness.app, "catalog", cursor=cursor)
    await wait_for(resumed, until_ready)
    assert resumed.names() == ["runtime", "ready"]
    disconnect()


async def test_a_notebook_stream_attaches_the_client(harness: Harness) -> None:
    notebook = await harness.host.notebook(harness.notebook_id)
    await analyzed(notebook)
    response, task, disconnect = subscribe(
        harness.app, "notebook", notebook_id=notebook.id
    )
    await wait_for(response, until_ready)
    # An empty notebook loads with marimo's one default cell.
    assert response.names() == [
        "notebook",
        "cell",
        "document",
        "analysis",
        "attachment",
        "ready",
    ]
    assert response.records[2]["data"]["document"]["order"] == [
        response.records[1]["data"]["cell"]["id"]
    ]
    assert response.records[4]["data"]["attachment"]["kind"] == "client"

    disconnect()
    await asyncio.wait_for(task, 5)
    # Subscribing starts no runtime.
    assert harness.backend().calls == []
    assert [
        event_name(e.event)
        for e in harness.host.events(notebook_id=notebook.id)
    ] == [
        "notebook",
        "cell",
        "document",
        "analysis",
        "ready",
    ]

    missing = await refused(harness.app, "notebook", notebook_id="nope")
    assert missing["status"] == 404
    assert missing["type"].endswith(":not-found")


IDENTIFIED = {
    "attachment_identity": "k1",
    "attachment_kind": "agent",
    "attachment_name": "Codex",
}


async def attachment_of(
    harness: Harness, **arguments: Any
) -> protocol.Attachment:
    """Subscribe to the notebook with `arguments` and return its attachment."""
    response, task, disconnect = subscribe(
        harness.app,
        "notebook",
        notebook_id=harness.notebook_id,
        types=["attachment"],
        **arguments,
    )
    await wait_for(response, until_ready)
    assert response.names() == ["attachment", "ready"]
    message = msgspec.convert(
        response.records[0]["data"], protocol.AttachmentMessage
    )
    disconnect()
    await asyncio.wait_for(task, 5)
    return message.attachment


async def test_attachment_arguments_name_the_client(harness: Harness) -> None:
    attachment = await attachment_of(harness, **IDENTIFIED)
    assert attachment.identity == "k1"
    assert attachment.kind == "agent"
    assert attachment.name == "Codex"


async def test_an_empty_attachment_name_is_null(harness: Harness) -> None:
    attachment = await attachment_of(harness, attachment_name="")
    assert attachment.name is None
    assert attachment.kind == "client"


async def test_a_reconnect_with_one_identity_attaches_fresh(
    harness: Harness,
) -> None:
    arguments = {
        "notebook_id": harness.notebook_id,
        "types": ["attachment"],
        **IDENTIFIED,
    }
    first, first_task, disconnect_first = subscribe(
        harness.app, "notebook", **arguments
    )
    await wait_for(first, until_ready)
    cursor = first.records[-1]["id"]
    old = first.records[0]["data"]["attachment"]
    disconnect_first()
    await asyncio.wait_for(first_task, 5)
    assert "attachment" not in [
        event_name(e.event) for e in harness.host.events()
    ]

    second, second_task, disconnect_second = subscribe(
        harness.app, "notebook", cursor=cursor, **arguments
    )
    await wait_for(second, until_ready)
    assert second.names() == ["attachment.removed", "attachment", "ready"]
    assert second.records[0]["data"]["id"] == old["id"]
    new = second.records[1]["data"]["attachment"]
    assert new["identity"] == old["identity"] == "k1"
    assert new["id"] != old["id"]

    disconnect_second()
    await asyncio.wait_for(second_task, 5)


async def test_custom_events_have_their_own_kind(
    harness: Harness,
) -> None:
    notebook = await harness.host.notebook(harness.notebook_id)
    custom, custom_task, disconnect_custom = subscribe(
        harness.app, "notebook", notebook_id=notebook.id, types=["custom"]
    )
    attachments, attach_task, disconnect_attach = subscribe(
        harness.app, "notebook", notebook_id=notebook.id, types=["attachment"]
    )
    await wait_for(custom, until_ready)
    await wait_for(attachments, until_ready)

    notebook.emit("marimo.test", {"hello": 1})
    await wait_for(custom, lambda response: "custom" in response.names())
    frame = next(r for r in custom.records if r.get("event") == "custom")
    message = msgspec.convert(frame["data"], protocol.CustomMessage)
    assert message.custom == protocol.Custom(
        type="marimo.test", data={"hello": 1}
    )
    assert "custom" not in attachments.names()

    disconnect_custom()
    disconnect_attach()
    await asyncio.gather(custom_task, attach_task)


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("attachment_identity", ""),
        ("attachment_kind", "Agent"),
        ("attachment_name", "n" * 257),
    ],
)
async def test_invalid_attachment_arguments_are_rejected(
    harness: Harness, argument: str, value: str
) -> None:
    notebook = await harness.host.notebook(harness.notebook_id)
    problem = await refused(
        harness.app,
        "notebook",
        notebook_id=harness.notebook_id,
        **{argument: value},
    )
    assert problem["status"] == 400
    assert problem["type"].endswith(":invalid-request")
    assert f"$.{argument}" in problem["detail"]
    assert notebook._attachments == {}


async def test_an_unexpected_failure_is_an_internal_problem(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("a secret detail")

    monkeypatch.setattr(harness.bridge, "edit", broken)
    edited = await call(
        harness.app,
        "notebook.edit",
        idempotency_key="edit",
        notebook_id=harness.notebook_id,
        changes=[],
    )
    assert edited.problem["type"].endswith(":internal")
    assert "secret" not in str(edited.problem)


async def test_runtime_lifecycle(harness: Harness) -> None:
    app, nid = harness.app, harness.notebook_id
    notebook = await harness.host.notebook(nid)

    async def lifecycle(name: str, key: str) -> Called:
        return await call(app, name, idempotency_key=key, notebook_id=nid)

    started = await lifecycle("runtime.start", "k1")
    assert started.data["status"] == "starting"
    assert harness.backend().calls == [("start", started.data["id"])]

    # The runtime it found, still starting.
    again = await lifecycle("runtime.start", "k2")
    assert again.data["id"] == started.data["id"]

    restart = await lifecycle("runtime.restart", "k3")
    assert restart.problem["type"].endswith(":conflict")

    notebook.observe(be.Running(runtime_id_of(notebook), "0.25.0"))
    restart = await lifecycle("runtime.restart", "k4")
    assert restart.data["status"] == "starting"
    assert restart.data["id"] != started.data["id"]

    notebook.observe(be.Exited(runtime_id_of(notebook)))
    notebook.observe(be.Running(runtime_id_of(notebook), "0.25.0"))
    stopped = await lifecycle("runtime.stop", "k5")
    assert stopped.data["status"] == "terminating"
    assert stopped.data["id"] == restart.data["id"]

    notebook.observe(be.Exited(runtime_id_of(notebook)))
    gone = await lifecycle("runtime.stop", "k6")
    assert gone.problem["type"].endswith(":no-runtime")


async def test_slow_restart_keeps_its_receipt(harness: Harness) -> None:
    nid = await harness.running()
    notebook = await harness.host.notebook(nid)

    async def restart() -> Called:
        return await call(
            harness.app,
            "runtime.restart",
            idempotency_key="restart",
            notebook_id=nid,
        )

    accepted = await restart()
    assert accepted.ok, accepted.problem

    # The receipt's 30-second window must not begin during shutdown/startup.
    harness.now = 100
    harness.app.state.host_context.mutations.expire()
    notebook.observe(be.Exited(runtime_id_of(notebook)))
    notebook.observe(be.Running(runtime_id_of(notebook), "0.25.0"))
    calls = list(harness.backend().calls)

    assert (await restart(), harness.backend().calls) == (accepted, calls)
    harness.now = 129
    assert (await restart(), harness.backend().calls) == (accepted, calls)
    harness.now = 130
    fresh = await restart()
    assert fresh.ok, fresh.problem
    assert fresh.data["id"] != accepted.data["id"]
    assert harness.backend().calls == [*calls, ("stop", accepted.data["id"])]


@pytest.mark.parametrize(
    ("operation", "failed"),
    [
        ("runtime.start", False),
        ("runtime.start", True),
        ("runtime.stop", False),
        ("runtime.restart", False),
        ("runtime.restart", True),
    ],
)
async def test_lifecycle_receipts_expire_after_terminal_events(
    harness: Harness, operation: str, failed: bool
) -> None:
    nid = harness.notebook_id
    if operation != "runtime.start":
        await harness.running()
    notebook = await harness.host.notebook(nid)
    context = harness.app.state.host_context
    listeners = list(harness.host._stream._listeners)

    async def lifecycle() -> Called:
        return await call(
            harness.app,
            operation,
            idempotency_key="lifecycle",
            notebook_id=nid,
        )

    accepted = await lifecycle()
    assert accepted.ok, accepted.problem
    for now in (100, 200):
        harness.now = now
        context.mutations.expire()
        calls = list(harness.backend().calls)
        assert (await lifecycle(), harness.backend().calls) == (
            accepted,
            calls,
        )
        if operation == "runtime.restart" and now == 100:
            notebook.observe(be.Exited(runtime_id_of(notebook)))
    rid = runtime_id_of(notebook)
    notebook.observe(
        be.Exited(rid, "failed" if failed else None)
        if failed or operation == "runtime.stop"
        else be.Running(rid)
    )
    assert harness.host._stream._listeners == listeners
    calls = list(harness.backend().calls)
    harness.now = 229
    context.mutations.expire()
    assert (await lifecycle(), harness.backend().calls) == (accepted, calls)
    harness.now = 230
    assert context.mutations.remembered("lifecycle") is None


@pytest.mark.parametrize(
    "operation", ["start", "stop", "restart", "replacement"]
)
async def test_stop_settles_superseded_lifecycle_receipts(
    harness: Harness, operation: str
) -> None:
    nid = harness.notebook_id
    if operation != "start":
        await harness.running()
    notebook = await harness.host.notebook(nid)
    context = harness.app.state.host_context
    listeners = list(harness.host._stream._listeners)
    pending = {
        "start": "runtime.start",
        "stop": "runtime.stop",
        "restart": "runtime.restart",
        "replacement": "runtime.restart",
    }[operation]

    async def lifecycle(name: str, key: str) -> Called:
        return await call(
            harness.app, name, idempotency_key=key, notebook_id=nid
        )

    accepted = await lifecycle(pending, "pending")
    if operation == "replacement":
        notebook.observe(be.Exited(runtime_id_of(notebook)))
    harness.now = 100
    stops = [
        await lifecycle("runtime.stop", key) for key in ("stop-1", "stop-2")
    ]
    harness.now = 200
    notebook.observe(be.Exited(runtime_id_of(notebook)))
    assert harness.host._stream._listeners == listeners
    calls = list(harness.backend().calls)
    harness.now = 229
    context.mutations.expire()
    for key, name, first in [
        ("pending", pending, accepted),
        ("stop-1", "runtime.stop", stops[0]),
        ("stop-2", "runtime.stop", stops[1]),
    ]:
        assert first.ok, first.problem
        replayed = await lifecycle(name, key)
        assert (replayed, harness.backend().calls) == (first, calls)
    harness.now = 230
    assert [
        context.mutations.remembered(key)
        for key in ("pending", "stop-1", "stop-2")
    ] == [None, None, None]


@pytest.mark.parametrize(
    "operation", ["start", "stop", "restart", "failed-stop"]
)
async def test_synchronous_lifecycle_completion(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    nid = harness.notebook_id
    if operation != "start":
        await harness.running()
    notebook = await harness.host.notebook(nid)
    if operation == "failed-stop":
        notebook.observe(be.Exited(runtime_id_of(notebook), "failed"))
    backend = harness.backend()
    start, stop = backend.start, backend.stop

    def start_immediately(rid: str) -> None:
        start(rid)
        notebook.observe(be.Running(protocol.RuntimeId(rid)))

    def stop_immediately(rid: str) -> None:
        stop(rid)
        notebook.observe(be.Exited(protocol.RuntimeId(rid)))

    monkeypatch.setattr(backend, "start", start_immediately)
    monkeypatch.setattr(backend, "stop", stop_immediately)
    listeners = list(harness.host._stream._listeners)
    name = {"start": "runtime.start", "restart": "runtime.restart"}.get(
        operation, "runtime.stop"
    )

    async def lifecycle() -> Called:
        return await call(
            harness.app, name, idempotency_key="immediate", notebook_id=nid
        )

    accepted = await lifecycle()
    assert accepted.ok, accepted.problem
    assert harness.host._stream._listeners == listeners
    calls = list(backend.calls)
    harness.now = 29
    assert (await lifecycle(), backend.calls) == (accepted, calls)
    harness.now = 30
    assert (
        harness.app.state.host_context.mutations.remembered("immediate")
        is None
    )


async def test_noop_and_rejected_lifecycle_requests_release_their_listeners(
    harness: Harness,
) -> None:
    nid = await harness.running()
    context = harness.app.state.host_context
    listeners = list(harness.host._stream._listeners)
    existing = await call(
        harness.app,
        "runtime.start",
        idempotency_key="existing",
        notebook_id=nid,
    )
    assert existing.data["status"] == "running"
    assert harness.host._stream._listeners == listeners
    rejected = await call(
        harness.app,
        "runtime.start",
        idempotency_key="invalid",
        notebook_id=nid,
        bogus=True,
    )
    assert rejected.problem["type"].endswith(":invalid-request")
    assert context.mutations.remembered("invalid") is None
    assert harness.host._stream._listeners == listeners
    harness.now = 30
    assert context.mutations.remembered("existing") is None


async def test_run_filter(harness: Harness) -> None:
    nid = await harness.running()
    notebook = await harness.host.notebook(nid)
    first = notebook.run("1", request_id="first", request=None)
    notebook.observe(
        be.Planned(
            first.id,
            (be.PlannedExecution(protocol.ExecutionId("e1"), None, "h", "1"),),
        )
    )
    notebook.observe(
        be.Completed(protocol.ExecutionId("e1"), protocol.ValueOutcome(None))
    )
    notebook.observe(be.Done(first.id, "succeeded"))
    notebook.run("2", request_id="second", request=None)
    response, task, disconnect = subscribe(
        harness.app,
        "notebook",
        notebook_id=nid,
        run_id=first.id,
        types=["run"],
    )
    try:
        await wait_for(response, until_ready)
        assert response.names() == ["run", "ready"]
        assert response.records[0]["data"]["run"]["id"] == first.id
    finally:
        disconnect()
        await asyncio.wait_for(task, 5)


async def test_interrupting_an_unstarted_run_after_shutdown(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    nid = await harness.running()
    accepted = await call(
        harness.app,
        "runtime.execute",
        idempotency_key="queued",
        notebook_id=nid,
        code="1",
    )
    assert accepted.ok, accepted.problem
    run_id = accepted.data["id"]
    notebook = await harness.host.notebook(nid)
    runtime_id = runtime_id_of(notebook)
    notebook.stop()
    notebook.observe(be.Exited(runtime_id))

    def snapshot_unavailable(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("run lookup must not build a snapshot")

    monkeypatch.setattr(harness.host, "events", snapshot_unavailable)

    async def cancel(key: str, run: str) -> str:
        cancelled = await call(
            harness.app,
            "runtime.cancel",
            idempotency_key=key,
            notebook_id=nid,
            run_id=run,
        )
        kind: str = cancelled.problem["type"]
        return kind.rsplit(":", 1)[1]

    assert await cancel("completed", run_id) == "conflict"
    assert await cancel("unknown", "unknown") == "not-found"
    harness.now = 61
    harness.host.expire()
    assert await cancel("expired", run_id) == "not-found"


@pytest.mark.parametrize("allowed", ["runtime.run", "runtime.execute"])
@pytest.mark.parametrize("scope", ["notebook", "host"])
async def test_execution_permissions_are_independent(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, allowed: str, scope: str
) -> None:
    nid = await harness.running()
    notebook = await harness.host.notebook(nid)
    (cell,) = notebook.document
    if scope == "notebook":
        harness.host.update_notebook(
            msgspec.structs.replace(
                harness.host.notebooks[nid], operations=(allowed,)
            )
        )
    else:
        monkeypatch.setattr(
            harness.host, "_host", protocol.Host(operations=(allowed,))
        )
    for operation, arguments in [
        ("runtime.run", {"cells": [{"id": cell.id}]}),
        ("runtime.execute", {"code": "1"}),
        ("notebook.edit", {"changes": []}),
    ]:
        calls_before = len(harness.backend().calls)
        result = await call(
            harness.app,
            operation,
            idempotency_key=operation,
            notebook_id=nid,
            **arguments,
        )
        if operation == allowed:
            assert result.ok, result.problem
            for name, more in [
                ("runtime.cancel", {"run_id": result.data["id"]}),
                ("runtime.interrupt", {}),
            ]:
                interrupted = await call(
                    harness.app,
                    name,
                    idempotency_key=name,
                    notebook_id=nid,
                    **more,
                )
                assert interrupted.ok, interrupted.problem
        else:
            assert result.problem["type"].endswith(
                ":forbidden"
                if scope == "notebook"
                else ":unsupported-operation"
            )
            assert len(harness.backend().calls) == calls_before


async def test_runtime_interrupt(harness: Harness) -> None:
    app, nid = harness.app, harness.notebook_id

    async def interrupt(key: str) -> Called:
        return await call(
            app, "runtime.interrupt", idempotency_key=key, notebook_id=nid
        )

    refused = await interrupt("i0")
    assert refused.problem["type"].endswith(":no-runtime")

    await harness.running()
    notebook = await harness.host.notebook(nid)
    accepted = await interrupt("i1")
    assert accepted.data["id"] == runtime_id_of(notebook)
    assert accepted.data["status"] == "running"
    assert harness.backend().calls[-1] == ("interrupt",)

    # A retry answers with the first acknowledgement and interrupts nothing.
    assert await interrupt("i1") == accepted
    assert harness.backend().calls.count(("interrupt",)) == 1


async def test_scratch_runs(harness: Harness) -> None:
    app, nid = harness.app, harness.notebook_id

    async def execute(key: str, code: str = "3 * 3") -> Called:
        return await call(
            app,
            "runtime.execute",
            idempotency_key=key,
            notebook_id=nid,
            code=code,
        )

    async def cancel(key: str, run_id: str) -> Called:
        return await call(
            app,
            "runtime.cancel",
            idempotency_key=key,
            notebook_id=nid,
            run_id=run_id,
        )

    refused = await execute("r0")
    assert refused.problem["type"].endswith(":no-runtime")

    await harness.running()
    accepted = await execute("r1")
    run_id = accepted.data["id"]
    assert accepted.data["status"] == "queued"
    assert harness.backend().calls[-1] == ("run", run_id, "3 * 3")

    assert await execute("r1") == accepted
    assert harness.backend().calls.count(("run", run_id, "3 * 3")) == 1
    notebook = await harness.host.notebook(nid)
    notebook.observe(
        be.Planned(run_id, (be.PlannedExecution("e1", None, "hash", "3 * 3"),))
    )
    notebook.observe(be.Started("e1"))
    assert (await execute("r1")).data["status"] == "running"
    reused = await execute("r1", "4")
    assert reused.problem["type"].endswith(":conflict")

    queued = await execute("r2")
    assert queued.data["status"] == "queued"
    assert queued.data["id"] != run_id
    assert harness.backend().calls[-1] == ("run", queued.data["id"], "3 * 3")

    interrupted = await cancel("i1", run_id)
    assert interrupted.ok, interrupted.problem
    assert harness.backend().calls[-1] == ("cancel", run_id)

    notebook.observe(be.Completed("e1", protocol.InterruptedOutcome()))
    notebook.observe(be.Done(run_id, "interrupted"))
    completed = (await execute("r1")).data
    assert completed["status"] == "completed"
    assert completed["outcome"] == "interrupted"
    assert completed["completed_at"] is not None
    assert (await cancel("i2", run_id)).problem["type"].endswith(":conflict")
    assert (await cancel("i3", "zz")).problem["type"].endswith(":not-found")

    invalid = await call(
        app, "runtime.execute", idempotency_key="r3", notebook_id=nid
    )
    assert invalid.problem["type"].endswith(":invalid-request")
    unkeyed = await call(app, "runtime.execute", notebook_id=nid, code="1")
    assert unkeyed.problem["type"].endswith(":invalid-request")


@pytest.mark.parametrize("editor_auth", [False, True])
@pytest.mark.parametrize("cell_run", [False, True])
async def test_a_run_hands_its_request_to_the_backend(
    harness: Harness,
    editor_auth: bool,
    cell_run: bool,
) -> None:
    nid = await harness.running()
    headers = {**AUTH, "X-Trace": "abc", "Cookie": "session=s"}
    query = "debug=1"
    if editor_auth:
        headers.update(
            {
                "Authorization": "Bearer application-token",
                "Marimo-Server-Token": "skew-id-1",
            }
        )
        query += (
            f"&access_token={harness.app.state.session_manager.auth_token}"
        )
    notebook = await harness.host.notebook(nid)
    endpoint = "/api/mcp" if editor_auth else f"{HOST_API_PATH}/mcp"
    accepted = await call(
        harness.app,
        "runtime.run" if cell_run else "runtime.execute",
        headers=headers,
        query=query,
        url=endpoint,
        idempotency_key="run",
        notebook_id=nid,
        **(
            {"cells": [{"id": cell.id} for cell in notebook.document]}
            if cell_run
            else {"code": "1"}
        ),
    )
    assert accepted.ok, accepted.problem
    [submitted] = harness.backend().requests
    assert submitted is not None
    # The run's request is the call of its tool.
    assert submitted.url["path"] == endpoint
    assert submitted.url["query"] == query
    assert submitted.headers["x-trace"] == "abc"
    if editor_auth:
        assert submitted.headers["authorization"] == "Bearer application-token"
    else:
        assert "authorization" not in submitted.headers
    assert submitted.query_params == parse_qs(query)
    assert submitted.path_params == {}
    assert submitted.cookies == {"session": "s"}


@pytest.mark.parametrize(
    "harness",
    [RetentionPolicy(ephemeral_seconds=10)],
    indirect=True,
)
@pytest.mark.parametrize("cell_id", [None, "a"])
async def test_request_receipts_expire_independently_of_results(
    harness: Harness, cell_id: str | None
) -> None:
    nid = await harness.running()
    notebook = await harness.host.notebook(nid)

    async def execute(code: str) -> Called:
        return await call(
            harness.app,
            "runtime.execute",
            idempotency_key="run",
            notebook_id=nid,
            code=code,
        )

    run_id = protocol.RunId((await execute("42")).data["id"])
    eid = protocol.ExecutionId("execution")
    notebook.observe(
        be.Planned(run_id, (be.PlannedExecution(eid, cell_id, "hash", "42"),))
    )
    notebook.observe(be.Started(eid))
    harness.now = 100
    active = (await execute("42")).data
    assert (active["id"], active["status"]) == (run_id, "running")
    notebook.observe(be.Completed(eid, protocol.ValueOutcome(None)))
    notebook.observe(be.Done(run_id, "succeeded"))
    harness.now = 129
    harness.host.expire()
    replayed = (await execute("42")).data
    assert (replayed["id"], replayed["status"], replayed["outcome"]) == (
        run_id,
        "completed",
        "succeeded",
    )
    assert any(
        isinstance(
            event.event,
            (protocol.ExecutionEvent, protocol.ExecutionRemovedEvent),
        )
        for event in harness.host.events(notebook_id=nid)
    ) == (cell_id is not None)
    assert (await execute("43")).problem["type"].endswith(":conflict")

    harness.now = 130
    fresh = await execute("43")
    assert fresh.ok, fresh.problem
    assert fresh.data["id"] != run_id
    assert [call for call in harness.backend().calls if call[0] == "run"] == [
        ("run", run_id, "42"),
        ("run", fresh.data["id"], "43"),
    ]


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_concurrent_retries_share_the_mutation(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, cancel_waiter: bool
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    create = harness.bridge.create

    async def delayed_create(
        target: Path, *, name: str | None, request_id: str
    ) -> protocol.Notebook:
        started.set()
        await release.wait()
        return await create(target, name=name, request_id=request_id)

    monkeypatch.setattr(harness.bridge, "create", delayed_create)

    def created(path: str) -> asyncio.Task[Called]:
        return asyncio.ensure_future(
            call(
                harness.app,
                "notebook.create",
                idempotency_key="create",
                project_id=harness.bridge.index.project_id,
                path=path,
            )
        )

    first = created("new.py")
    await asyncio.wait_for(started.wait(), 5)
    try:
        conflicting = await created("other.py")
        assert conflicting.problem["type"].endswith(":conflict")
        if cancel_waiter:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        retry = created("new.py")
        # Queue the release after the retry has entered the request handler.
        asyncio.get_running_loop().call_soon(release.set)
        retried = await asyncio.wait_for(retry, 5)
        assert retried.ok, retried.problem
        if not cancel_waiter:
            assert await asyncio.wait_for(first, 5) == retried
        assert (harness.root / "new.py").exists()
        assert not (harness.root / "other.py").exists()
        assert sorted(n.path for n in harness.host.notebooks.values()) == [
            "forecasts.py",
            "new.py",
        ]
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_rejected_mutations_do_not_reserve_the_key(
    harness: Harness,
) -> None:
    async def move(path: str) -> Called:
        return await call(
            harness.app,
            "notebook.update",
            idempotency_key="move",
            notebook_id=harness.notebook_id,
            path=path,
        )

    assert (await move("forecasts.py")).problem["type"].endswith(":conflict")
    corrected = await move("corrected.py")
    assert corrected.data["path"] == "corrected.py"
    assert (harness.root / "corrected.py").exists()
    assert not (harness.root / "forecasts.py").exists()


async def test_create_update_and_delete_notebooks(harness: Harness) -> None:
    app, root = harness.app, harness.root
    project = harness.bridge.index.project_id

    async def create(key: str, path: str, project_id: str = project) -> Called:
        return await call(
            app,
            "notebook.create",
            idempotency_key=key,
            project_id=project_id,
            path=path,
        )

    created = await create("c1", "new/one.py")
    assert created.ok, created.problem
    record = created.data
    assert record["path"] == str(Path("new") / "one.py")
    assert record["name"] == "one"
    assert (root / "new" / "one.py").read_text().startswith("import marimo")
    assert record["id"] in harness.host.notebooks

    for failed, kind in [
        (await create("c2", "new/one.py"), "conflict"),
        (await create("c3", "../x.py"), "invalid-request"),
        (await create("c4", "y.py", "nope"), "not-found"),
    ]:
        assert failed.problem["type"].endswith(f":{kind}")

    moved = await call(
        app,
        "notebook.update",
        idempotency_key="u1",
        notebook_id=record["id"],
        path="two.py",
        name="Two",
    )
    assert moved.ok, moved.problem
    assert moved.data["path"] == "two.py"
    assert moved.data["name"] == "Two"
    assert (root / "two.py").exists()
    assert not (root / "new" / "one.py").exists()
    harness.bridge.index.refresh()
    assert harness.host.notebooks[record["id"]].name == "Two"

    onto = await call(
        app,
        "notebook.update",
        idempotency_key="u2",
        notebook_id=record["id"],
        path="forecasts.py",
    )
    assert onto.problem["type"].endswith(":conflict")

    async def delete(key: str, notebook_id: str) -> Called:
        return await call(
            app,
            "notebook.delete",
            idempotency_key=key,
            notebook_id=notebook_id,
        )

    deleted = await delete("d1", record["id"])
    assert deleted == Called(True, {})
    assert not (root / "two.py").exists()
    assert record["id"] not in harness.host.notebooks
    assert await delete("d1", record["id"]) == deleted

    await harness.running()
    busy = await delete("d2", harness.notebook_id)
    assert busy.problem["type"].endswith(":conflict")


async def test_open_selects_the_notebook_outside_the_working_directory(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = harness.root / "forecasts.py"
    workspace = SingleFileWorkspace(MarimoFile(name=file.name, path=str(file)))
    harness.app.state.session_manager.workspace = workspace
    launch = harness.root / "launch"
    launch.mkdir()
    (launch / file.name).write_text("another notebook")
    monkeypatch.chdir(launch)
    opened: list[str] = []
    ready = threading.Event()

    def open_browser(_browser: str, url: str) -> None:
        opened.append(url)
        ready.set()

    monkeypatch.setattr(api_module, "open_url_in_browser", open_browser)
    response = await call(
        harness.app,
        "notebook.open",
        idempotency_key="o1",
        notebook_id=harness.notebook_id,
    )

    assert response.ok, response.problem
    url = response.data["url"]
    query = parse_qs(urlsplit(url).query)
    assert workspace.resolve(query["file"][0]) == str(file)
    assert query["access_token"] == ["fake-token"]
    assert TOKEN not in url
    assert await asyncio.to_thread(ready.wait, 5)
    assert opened == [url]


async def test_operations_the_host_does_not_offer_are_501(
    harness: Harness,
) -> None:
    notebook = await harness.host.notebook(harness.notebook_id)
    host = Host(operations=[])
    [project] = [
        e.event.data.project
        for e in harness.host.events()
        if isinstance(e.event.data, ProjectMessage)
    ]
    host.update_project(project)
    host.add_notebook(harness.host.notebooks[notebook.id], make_notebook)
    harness.bridge.host = host
    harness.app.state.host_context = HostContext(
        bridge=harness.bridge, token=TOKEN, port=PORT
    )
    response = await call(
        harness.app,
        "runtime.start",
        idempotency_key="x1",
        notebook_id=notebook.id,
    )
    assert response.problem["status"] == 501
    assert response.problem["type"].endswith(":unsupported-operation")


async def test_competing_moves_preserve_both_notebooks(
    harness: Harness,
) -> None:
    first = harness.root / "forecasts.py"
    second = harness.root / "second.py"
    first.write_text("first notebook")
    second.write_text("second notebook")
    other = harness.bridge.index.ensure(str(second))
    assert other is not None
    ids = [harness.notebook_id, other.id]

    moves = await asyncio.gather(
        *(
            call(
                harness.app,
                "notebook.update",
                idempotency_key=f"move-{i}",
                notebook_id=nid,
                path="destination.py",
            )
            for i, nid in enumerate(ids)
        )
    )

    assert sorted(move.ok for move in moves) == [False, True]
    records = [harness.host.notebooks[nid] for nid in ids]
    assert len({record.path for record in records}) == 2
    assert {
        (harness.root / record.path).read_text()
        for record in records
        if record.path is not None
    } == {"first notebook", "second notebook"}


@pytest.mark.parametrize("operation", ["create", "move"])
async def test_symlink_escape_has_no_filesystem_side_effects(
    harness: Harness, tmp_path: Path, operation: str
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (harness.root / "link").symlink_to(outside, target_is_directory=True)
    source = harness.root / "forecasts.py"
    original = source.read_text()
    target = (
        {"project_id": harness.bridge.index.project_id}
        if operation == "create"
        else {"notebook_id": harness.notebook_id}
    )
    response = await call(
        harness.app,
        "notebook.create" if operation == "create" else "notebook.update",
        idempotency_key="escape",
        path="link/nested/new.py",
        **target,
    )
    assert response.problem["type"].endswith(":conflict")
    assert list(outside.iterdir()) == []
    assert source.read_text() == original
    assert [n.path for n in harness.host.notebooks.values()] == [
        "forecasts.py"
    ]


NOTEBOOK = """import marimo

__generated_with = "0.0.0"
app = marimo.App(width="full")


@app.cell
def _():
    x = 21
    return (x,)


@app.cell(hide_code=True)
def _(x):
    y = x * 2
    return


if __name__ == "__main__":
    app.run()
"""


@pytest.mark.parametrize("preanalyzed", [False, True])
async def test_the_notebook_stream_carries_cells_then_the_document(
    harness: Harness,
    preanalyzed: bool,
) -> None:
    (harness.root / "forecasts.py").write_text(NOTEBOOK)
    if preanalyzed:
        await analyzed(await harness.host.notebook(harness.notebook_id))
    response, _, disconnect = subscribe(
        harness.app, "notebook", notebook_id=harness.notebook_id
    )
    await wait_for(
        response, lambda r: until_ready(r) and "analysis" in r.names()
    )
    disconnect()

    # Analysis follows the document, but can finish before or after ready.
    names = response.names()
    assert [name for name in names if name != "analysis"] == [
        "notebook",
        "cell",
        "cell",
        "document",
        "attachment",
        "ready",
    ]
    assert names.count("analysis") == 1
    assert names.index("analysis") > names.index("document")
    first, second = (r["data"]["cell"] for r in response.records[1:3])
    document = response.records[3]["data"]["document"]
    analysis = response.records[names.index("analysis")]["data"]["analysis"]
    assert first["code"] == "x = 21"
    assert second["code"] == "y = x * 2"
    assert second["config"]["hide_code"] is True
    assert document["order"] == [first["id"], second["id"]]
    assert document["app_config"] == {"width": "full"}
    assert "policy" not in document
    assert document["saved_etag"] == document["etag"]
    assert all(
        c["notebook_id"] == harness.notebook_id for c in (first, second)
    )
    assert analysis["notebook_id"] == harness.notebook_id
    assert analysis["document_etag"] == document["etag"]
    assert analysis["cells"] == {
        first["id"]: {"defs": ["x"], "refs": [], "errors": []},
        second["id"]: {"defs": ["y"], "refs": ["x"], "errors": []},
    }

    # A filtered subscription is a snapshot of the document alone, and a
    # second one reuses the loaded document.
    response, _, disconnect = subscribe(
        harness.app,
        "notebook",
        notebook_id=harness.notebook_id,
        types=["cell", "document"],
    )
    await wait_for(response, until_ready)
    disconnect()
    assert response.names() == ["cell", "cell", "document", "ready"]
    assert response.records[2]["data"]["document"] == document


async def test_an_unreadable_notebook_stays_in_the_catalog_and_can_retry(
    harness: Harness,
) -> None:
    file = harness.root / "forecasts.py"
    file.write_text("print('not a notebook')\n")
    notebook_id = harness.notebook_id

    problem = await refused(harness.app, "notebook", notebook_id=notebook_id)
    assert problem["status"] == 409
    assert harness.host.peek_notebook(notebook_id) is None
    assert notebook_id in harness.host.notebooks
    assert harness.backends == {}

    file.write_text(NOTEBOOK)
    notebook = await harness.host.notebook(notebook_id)
    assert [cell.code for cell in notebook.document] == [
        "x = 21",
        "y = x * 2",
    ]


async def test_deleting_an_unreadable_notebook_does_not_initialize_it(
    harness: Harness,
) -> None:
    file = harness.root / "forecasts.py"
    file.write_text("print('not a notebook')\n")
    notebook_id = harness.notebook_id
    response = await call(
        harness.app,
        "notebook.delete",
        idempotency_key="delete-unreadable",
        notebook_id=notebook_id,
    )
    assert response.ok, response.problem
    assert notebook_id not in harness.host.notebooks
    assert not file.exists()
    assert harness.backends == {}


async def edit(harness: Harness, key: str, *changes: dict[str, Any]) -> Called:
    return await call(
        harness.app,
        "notebook.edit",
        idempotency_key=key,
        notebook_id=harness.notebook_id,
        changes=list(changes),
    )


async def save(harness: Harness, key: str, **arguments: Any) -> Called:
    return await call(
        harness.app,
        "notebook.save",
        idempotency_key=key,
        notebook_id=harness.notebook_id,
        **arguments,
    )


async def opened(harness: Harness) -> tuple[Response, Callable[[], None]]:
    """Subscribe to the notebook past `ready`, returning it and its closer."""
    (harness.root / "forecasts.py").write_text(NOTEBOOK)
    response, _, disconnect = subscribe(
        harness.app, "notebook", notebook_id=harness.notebook_id
    )
    await wait_for(response, until_ready)
    return response, disconnect


def first_record(stream: Response, event: str) -> dict[str, Any]:
    return next(r["data"] for r in stream.records if r.get("event") == event)


async def test_an_edit_reaches_the_stream_as_one_unit(
    harness: Harness,
) -> None:
    stream, disconnect = await opened(harness)
    document = first_record(stream, "document")["document"]
    first_id = document["order"][0]
    # The first revision's analysis arrives off the event loop.
    await wait_for(stream, lambda r: "analysis" in r.names())
    seen = len(stream.records)

    edited = await edit(
        harness,
        "edit-1",
        {
            "kind": "put_cell",
            "base_etag": None,
            "after": first_id,
            "cell": {"id": "fresh", "code": "z = 3"},
        },
    )
    assert edited.ok, edited.problem
    response = edited.data
    assert set(response["cells"]) == {"fresh"}
    assert response["document"]["order"][1] == "fresh"
    assert response["document"]["saved_etag"] == document["etag"]
    assert response["document"]["etag"] != document["etag"]

    await wait_for(stream, lambda r: r.names().count("analysis") >= 2)
    after = stream.records[seen:]
    assert [r["event"] for r in after] == ["cell", "document", "analysis"]
    assert all(r["data"]["request_id"] == "edit-1" for r in after)
    assert (
        after[0]["data"]["cell"]["etag"] == response["cells"]["fresh"]["etag"]
    )
    assert after[1]["data"]["document"] == response["document"]
    analysis = after[2]["data"]["analysis"]
    assert analysis["document_etag"] == response["document"]["etag"]
    assert analysis["cells"]["fresh"]["defs"] == ["z"]
    assert "z = 3" not in (harness.root / "forecasts.py").read_text()
    disconnect()


async def test_a_stale_etag_fails_the_precondition_and_changes_nothing(
    harness: Harness,
) -> None:
    stream, disconnect = await opened(harness)
    disconnect()
    cell = first_record(stream, "cell")["cell"]
    document = first_record(stream, "document")["document"]

    refused = await edit(
        harness,
        "edit-stale",
        {
            "kind": "put_cell",
            "base_etag": None,
            "cell": {"id": "new", "code": "1"},
        },
        {
            "kind": "put_cell",
            "base_etag": "0000000000000000",
            "cell": {"id": cell["id"], "code": "x = 99"},
        },
    )
    problem = refused.problem
    assert problem["status"] == 412
    assert problem["type"].endswith(":precondition-failed")
    assert problem["index"] == 1
    assert problem["cell"]["etag"] == cell["etag"]
    assert problem["document"]["etag"] == document["etag"]

    invalid = await edit(
        harness,
        "edit-invalid",
        {"kind": "set_order", "base_etag": "*", "order": [cell["id"]]},
    )
    assert invalid.problem["type"].endswith(":invalid-request")

    again, disconnect = await opened(harness)
    disconnect()
    assert (
        first_record(again, "document")["document"]["etag"] == document["etag"]
    )


async def test_save_writes_the_file_and_reports_the_saved_revision(
    harness: Harness,
) -> None:
    stream, disconnect = await opened(harness)
    document = first_record(stream, "document")["document"]
    first_id = document["order"][0]
    edited = await edit(
        harness,
        "edit-1",
        {
            "kind": "put_cell",
            "base_etag": None,
            "after": first_id,
            "cell": {"id": "fresh", "code": "z = 3"},
        },
    )
    assert edited.ok, edited.problem
    etag = edited.data["document"]["etag"]
    assert "z = 3" not in (harness.root / "forecasts.py").read_text()

    stale = await save(harness, "save-stale", expected_etag=document["etag"])
    assert stale.problem["type"].endswith(":precondition-failed")
    assert stale.problem["index"] == 0
    assert stale.problem["document"]["etag"] == etag

    saved = await save(harness, "save-1", expected_etag=etag)
    assert saved.ok, saved.problem
    assert saved.data["document"]["saved_etag"] == etag
    text = (harness.root / "forecasts.py").read_text()
    assert "z = 3" in text
    assert 'width="full"' in text
    await wait_for(
        stream,
        lambda r: any(
            x.get("event") == "document"
            and x["data"]["document"]["saved_etag"] == etag
            and x["data"]["request_id"] == "save-1"
            for x in r.records
        ),
    )

    # A retry under the same key with another precondition is another call.
    again = await save(harness, "save-1", expected_etag=document["etag"])
    assert again.problem["type"].endswith(":conflict")
    assert "another request" in again.problem["detail"]
    disconnect()


async def test_edits_during_a_save_remain_unsaved(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook = await harness.host.notebook(harness.notebook_id)
    document = notebook.document
    written = document.etag
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = AppFileManager.save_from_cells

    def write(self: AppFileManager, cells: Any, **kwargs: Any) -> str:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(self, cells, **kwargs)

    monkeypatch.setattr(AppFileManager, "save_from_cells", write)
    saving = asyncio.ensure_future(save(harness, "save-before-edit"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        edited = await edit(
            harness,
            "edit-during-save",
            {
                "kind": "put_cell",
                "base_etag": None,
                "cell": {"id": "new", "code": "new_value = 42"},
            },
        )
        assert edited.ok, edited.problem
    finally:
        release.set()
        saved = await asyncio.wait_for(saving, 5)

    assert saved.ok, saved.problem
    assert saved.data["document"] == {
        **edited.data["document"],
        "saved_etag": written,
    }
    assert "new_value" not in (harness.root / "forecasts.py").read_text()


async def test_a_file_changed_outside_marimo_needs_a_decision(
    harness: Harness,
) -> None:
    stream, disconnect = await opened(harness)
    disconnect()
    cell = first_record(stream, "cell")["cell"]
    file = harness.root / "forecasts.py"
    edited = await edit(
        harness,
        "edit-1",
        {
            "kind": "put_cell",
            "base_etag": cell["etag"],
            "cell": {"id": cell["id"], "code": "x = 99"},
        },
    )
    assert edited.ok, edited.problem
    file.write_text(file.read_text().replace("x = 21", "x = 22"))

    asked = await save(harness, "save-1")
    problem = asked.problem
    assert problem["status"] == 409
    assert problem["type"].endswith(":decision-required")
    assert [d["id"] for d in problem["decisions"]] == ["file"]
    assert [o["id"] for o in problem["decisions"][0]["options"]] == [
        "overwrite",
        "reload",
    ]
    assert "x = 22" in file.read_text()

    unknown = await save(harness, "save-2", decisions={"file": "merge"})
    assert unknown.problem["type"].endswith(":invalid-request")

    reloaded = await save(harness, "save-3", decisions={"file": "reload"})
    assert reloaded.ok, reloaded.problem
    response = reloaded.data
    assert response["document"]["saved_etag"] == response["document"]["etag"]
    assert set(response["cells"]) == {cell["id"]}
    document = (await harness.host.notebook(harness.notebook_id)).document
    assert document.cell(cell["id"]).code == "x = 22"  # type: ignore[union-attr]
    assert "x = 22" in file.read_text()

    # With the document back in step with the file, a plain save proceeds.
    edited = await edit(
        harness,
        "edit-2",
        {
            "kind": "put_cell",
            "base_etag": "*",
            "cell": {"id": cell["id"], "code": "x = 100"},
        },
    )
    assert edited.ok, edited.problem
    file.write_text(file.read_text().replace("x = 22", "x = 23"))
    overwritten = await save(
        harness, "save-4", decisions={"file": "overwrite"}
    )
    assert overwritten.ok, overwritten.problem
    assert "x = 100" in file.read_text()
    assert (await save(harness, "save-5")).ok

    # The host's own writes are not changes, even when the file does not
    # read back as written: a new cell's id is not stored in the file.
    for n, base, code in ((6, None, "y = 1\n\n"), (7, "*", "y = 2")):
        edited = await edit(
            harness,
            f"edit-{n}",
            {
                "kind": "put_cell",
                "base_etag": base,
                "cell": {"id": "fresh", "code": code},
            },
        )
        assert edited.ok, edited.problem
        assert (await save(harness, f"save-{n}")).ok


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (
            {},
            [
                ("marimo", "r2"),
                ("marimo", "r1"),
                ("custom", None),
                ("marimo", "r1"),
            ],
        ),
        (
            {"types": ["marimo"]},
            [("marimo", "r2"), ("marimo", "r1"), ("marimo", "r1")],
        ),
        (
            {"types": ["marimo"], "run_id": "r1"},
            [("marimo", "r1"), ("marimo", "r1")],
        ),
    ],
)
async def test_live_events_follow_the_notebook_and_the_filters(
    harness: Harness,
    filters: dict[str, Any],
    expected: list[tuple[str, str | None]],
) -> None:
    notebook = await harness.host.notebook(harness.notebook_id)
    await analyzed(notebook)

    def completed(notebook_id: str, run_id: str) -> RoutedEvent:
        return notebook.sink.append(
            notification_event(
                protocol.NotebookId(notebook_id),
                CompletedRunNotification(run_id=run_id),
            )
        )

    response, task, disconnect = subscribe(
        harness.app, "notebook", notebook_id=notebook.id, **filters
    )
    try:
        await wait_for(response, until_ready)
        seen = len(response.records)
        completed("nb_other", "r1")
        completed(notebook.id, "r2")
        completed(notebook.id, "r1")
        notebook.emit("test.message", {"text": "hello"})
        last = completed(notebook.id, "r1")
        await wait_for(
            response, lambda r: r.records[-1].get("id") == last.event.id
        )
        live = response.records[seen:]
        assert [
            (r["event"], r["data"].get("marimo", {}).get("run_id"))
            for r in live
        ] == expected
        assert live[-1] == {
            "id": last.event.id,
            "event": "marimo",
            "data": {
                "marimo": {
                    "op": "completed-run",
                    "run_id": "r1",
                    "outcome": "succeeded",
                    "error": None,
                }
            },
        }
        assert [
            r["data"]["custom"] for r in live if r["event"] == "custom"
        ] == (
            []
            if filters
            else [{"type": "test.message", "data": {"text": "hello"}}]
        )
    finally:
        disconnect()
        await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("base_url", ["", "/app"])
@pytest.mark.parametrize("credential", ["cookie", "query"])
@pytest.mark.parametrize(
    ("authority", "address", "rejected_status"),
    [
        (AUTHORITY, LOOPBACK, 401),
        ("notebooks.example:1234", ("192.0.2.1", 50000), 403),
    ],
)
def test_editor_authentication_is_limited_to_the_editor_tools(
    harness: Harness,
    base_url: str,
    credential: str,
    authority: str,
    address: tuple[str, int],
    rejected_status: int,
) -> None:
    app = create_starlette_app(base_url=base_url, enable_auth=True)
    manager = harness.app.state.session_manager
    get_starlette_server_state_init(
        session_manager=manager, base_url=base_url
    ).apply(app.state)
    app.state.host_context = harness.app.state.host_context
    with TestClient(
        with_client_address(app, address), base_url=f"http://{authority}"
    ) as client:
        query = ""
        if credential == "cookie":
            response = client.post(
                f"{base_url}/auth/login",
                data={"password": str(manager.auth_token)},
                follow_redirects=False,
            )
            assert response.status_code == 302
            assert client.cookies
        else:
            query = f"?access_token={manager.auth_token}"
        assert client.portal is not None
        notebook = client.portal.call(
            harness.host.notebook, harness.notebook_id
        )
        host = f"{base_url}{HOST_API_PATH}"

        def editor_call(name: str, **arguments: Any) -> Called:
            return call_tool(
                client,
                name,
                url=f"{base_url}/api/mcp{query}",
                headers={"Marimo-Server-Token": "skew-id-1"},
                idempotency_key="editor-1",
                **arguments,
            )

        edited = editor_call(
            "notebook.edit",
            notebook_id=notebook.id,
            changes=[
                {
                    "kind": "put_cell",
                    "base_etag": None,
                    "cell": {"id": "fresh", "code": "z = 3"},
                }
            ],
        )
        assert edited.ok, edited.problem
        assert set(edited.data["cells"]) == {"fresh"}
        # The editor serves no catalog tool, and its credentials open none
        # of the host's.
        moved = editor_call(
            "notebook.update", notebook_id=notebook.id, path="other.py"
        )
        assert moved.problem["message"] == "Unknown tool notebook.update"
        subscribed = client.post(
            f"{host}/mcp{query}",
            json=stream_payload("catalog", {}),
            headers=stream_headers("catalog"),
        )
        assert subscribed.status_code == rejected_status
        host_call = call_tool(
            client,
            "notebook.update",
            url=f"{host}/mcp{query}",
            idempotency_key="editor-2",
            notebook_id=notebook.id,
            path="other.py",
        )
        assert host_call.problem["status"] == rejected_status


@pytest.mark.parametrize(
    "name", ["notebook.edit", "runtime.start", "runtime.stop"]
)
@pytest.mark.parametrize(
    ("query", "headers", "refusal"),
    [
        # A page built for another server sends another token.
        (
            "?access_token=fake-token",
            {"Marimo-Server-Token": "stale"},
            {"error": "Invalid server token"},
        ),
        (
            "",
            {"Marimo-Server-Token": "skew-id-1"},
            problem("unauthorized", "Authorization header required"),
        ),
    ],
)
def test_editor_tools_need_credentials_and_the_server_token(
    harness: Harness,
    name: str,
    query: str,
    headers: dict[str, str],
    refusal: Any,
) -> None:
    client = TestClient(
        with_client_address(harness.app, ("192.0.2.1", 50000)),
        base_url="http://notebooks.example:1234",
    )
    called = call_tool(
        client,
        name,
        url=f"/api/mcp{query}",
        headers=headers,
        idempotency_key="editor-1",
        notebook_id=harness.notebook_id,
    )
    assert called.problem == msgspec.to_builtins(refusal)
    assert harness.backends == {}


@pytest.mark.parametrize("base_url", ["", "/app"])
@pytest.mark.parametrize(
    ("origin", "allowed"),
    [("http://editor.example", True), ("http://other.example", False)],
)
def test_editor_tool_preflights(
    harness: Harness, base_url: str, origin: str, allowed: bool
) -> None:
    app = create_starlette_app(
        base_url=base_url, allow_origins=("http://editor.example",)
    )
    get_starlette_server_state_init(
        session_manager=harness.app.state.session_manager, base_url=base_url
    ).apply(app.state)
    app.state.host_context = harness.app.state.host_context
    with TestClient(
        with_client_address(app, ("192.0.2.1", 50000)),
        base_url="http://notebooks.example:1234",
    ) as client:
        response = client.options(
            f"{base_url}/api/mcp",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "content-type,mcp-method,mcp-name,marimo-server-token"
                ),
            },
        )
    assert (
        response.status_code,
        response.headers.get("access-control-allow-origin"),
    ) == ((200, origin) if allowed else (400, None))
    assert harness.backends == {}


async def test_the_host_needs_its_token_without_editor_authentication(
    harness: Harness,
) -> None:
    app = create_starlette_app(base_url="", enable_auth=False)
    get_starlette_server_state_init(
        session_manager=harness.app.state.session_manager
    ).apply(app.state)
    app.state.enable_auth = False
    app.state.host_context = harness.app.state.host_context
    assert (await refused(app, "catalog", headers={}))["status"] == 401
    assert harness.backends == {}


async def test_cell_runs(harness: Harness) -> None:
    nid = await harness.running()
    notebook = await harness.host.notebook(nid)
    (cell,) = notebook.document

    async def run(key: str, **arguments: Any) -> Called:
        return await call(
            harness.app,
            "runtime.run",
            idempotency_key=key,
            notebook_id=nid,
            **arguments,
        )

    accepted = await run("r1", cells=[{"id": cell.id, "etag": cell.etag}])
    assert accepted.ok, accepted.problem
    run_id = accepted.data["id"]
    assert harness.backend().calls[-1] == (
        "run_cells",
        run_id,
        notebook.document.etag,
        (cell.id,),
    )

    notebook.observe(be.Done(protocol.RunId(run_id), "succeeded"))
    conflict = (
        await run("r2", cells=[{"id": cell.id, "etag": "0" * 16}])
    ).problem
    assert conflict["status"] == 412
    assert conflict["type"].endswith(":precondition-failed")
    assert "changed since it was read" in conflict["detail"]
    assert conflict["index"] == 0
    assert conflict["cell"]["etag"] == cell.etag
    assert conflict["document"]["etag"] == notebook.document.etag
    missing = (
        await run("missing", cells=[{"id": cell.id}, {"id": "ghost"}])
    ).problem
    assert missing["type"].endswith(":precondition-failed")
    assert missing["index"] == 1
    assert missing["cell"] is None
    assert missing["document"]["etag"] == notebook.document.etag

    both = await run("r3", code="1", cells=[{"id": cell.id}])
    assert both.problem["type"].endswith(":invalid-request")


def test_legacy_execution_does_not_start_a_cataloged_notebook(
    harness: Harness,
) -> None:
    with TestClient(
        with_client_address(harness.app), base_url=f"http://{AUTHORITY}"
    ) as client:
        response = client.post(
            "/api/kernel/execute",
            headers={**AUTH, "Marimo-Stable-Session-Id": harness.notebook_id},
            json={"code": "print('do not start')"},
        )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "The notebook has no running session."
    }
    assert harness.backends == {}
    assert "set-cookie" not in response.headers
