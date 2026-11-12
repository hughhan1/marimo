# Copyright 2026 Marimo. All rights reserved.
"""Input prompts and replies through the host's tools and a real kernel."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from marimo._messaging.notification import ConsumerCapabilities
from marimo._types.ids import ConsumerId, SessionId
from tests._server.api.endpoints.ws_helpers import headers, receive_until
from tests._server.host.drive import AUTH, call, socket_snapshot, wait_for
from tests._server.host.live import open_host
from tests._server.mcp_client import call_tool
from tests._server.mocks import get_session_manager
from tests._server.session_events import connect_session

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.testclient import TestClient

    from tests._server.host.drive import Response


@pytest.mark.parametrize("kiosk", [False, True])
def test_editor_input_replies_require_interact(
    client: TestClient, kiosk: bool
) -> None:
    with (
        client,
        connect_session(
            client, "/ws?session_id=editor", headers=headers("editor")
        ) as editor,
    ):
        snapshot = socket_snapshot(editor)
        notebook_id = next(
            event["data"]["notebook"]["id"]
            for event in snapshot
            if event["event"] == "notebook"
        )
        receive_until("kernel-ready", editor)
        url = "/ws?session_id=other" + ("&kiosk=true" if kiosk else "")
        with connect_session(client, url, headers("other")) as other:
            ready = receive_until("kernel-ready", other)
            assert ready["data"]["consumer_capabilities"] == {
                "edit": False,
                "interact": True,
            }
            accepted = call_tool(
                client,
                "runtime.execute",
                headers=headers("editor"),
                idempotency_key="run",
                notebook_id=notebook_id,
                code="assert input('Prompt: ') == 'answer'",
            )
            assert accepted.ok, accepted.problem
            run_id = accepted.data["id"]
            while True:
                event = editor.receive_json()
                if event["event"] == "run":
                    run = event["data"]["run"]
                    if run["id"] == run_id:
                        assert run["status"] != "completed", run
                if event["event"] == "input":
                    pending = event["data"]["input"]
                    break
            assert pending["status"] == "pending"
            reply_to = {
                "notebook_id": notebook_id,
                "input_id": pending["id"],
            }

            session = get_session_manager(client).get_session(
                SessionId("other")
            )
            assert session is not None
            consumer = session.room.get_consumer(ConsumerId("other"))
            assert consumer is not None
            session.room.add_consumer(
                consumer, main=False, capabilities=ConsumerCapabilities.VIEWER
            )
            for connection in ("other", "never-connected"):
                refused = call_tool(
                    client,
                    "runtime.input",
                    headers=headers(connection),
                    idempotency_key=f"refused-{connection}",
                    value="wrong answer",
                    **reply_to,
                )
                assert refused.problem["type"] == (
                    "urn:marimo:problem:forbidden"
                ), refused
            session.room.add_consumer(
                consumer,
                main=False,
                capabilities=ConsumerCapabilities.INTERACTOR,
            )
            reply = call_tool(
                client,
                "runtime.input",
                headers=headers("other"),
                idempotency_key="answer",
                value="answer",
                **reply_to,
            )
            assert reply.ok, reply.problem
            assert reply.data == {**pending, "status": "answered"}
            while True:
                event = editor.receive_json()
                if event["event"] == "run":
                    run = event["data"]["run"]
                    if run["id"] == run_id and run["status"] == "completed":
                        break
            assert run["outcome"] == "succeeded"


@pytest.mark.parametrize("scratch", [False, True])
async def test_input_replies_and_retries(
    tmp_path: Path, scratch: bool
) -> None:
    code = "import getpass\n_secret = getpass.getpass('Password: ')\n_blank = input('Empty: ')\nassert _secret == 'secret' and _blank == ''"
    source = (
        "import marimo\napp = marimo.App()\n@app.cell\ndef _():\n"
        + "\n".join("    " + line for line in code.splitlines())
        + "\n    return\n"
    )
    async with open_host(tmp_path, source) as (app, notebook_id, events):
        context = app.state.host_context
        notebook = await context.bridge.host.notebook(notebook_id)
        session_id = next(iter(app.state.session_manager.sessions))
        payload: dict[str, Any] = (
            {"code": code}
            if scratch
            else {"cells": [{"id": next(iter(notebook.document)).id}]}
        )
        accepted = await call(
            app,
            "runtime.execute" if scratch else "runtime.run",
            headers={**AUTH, "Marimo-Session-Id": session_id},
            idempotency_key="run",
            notebook_id=notebook_id,
            **payload,
        )
        assert accepted.ok, accepted.problem

        def prompts(response: Response) -> list[dict[str, Any]]:
            return [
                record["data"]["input"]
                for record in response.records
                if record.get("event") == "input"
                and record["data"]["input"]["status"] == "pending"
            ]

        def ended(response: Response) -> bool:
            return any(
                r.get("event") == "run"
                and r["data"]["run"]["status"] == "completed"
                for r in response.records
            )

        for index, value in enumerate(("secret", "")):

            def prompted(response: Response, index: int = index) -> bool:
                return len(prompts(response)) > index or ended(response)

            await wait_for(events, prompted)
            assert len(prompts(events)) > index, json.dumps(
                [
                    r["data"]["execution"]["outcome"]
                    for r in events.records
                    if r.get("event") == "execution"
                ]
            )
            pending = prompts(events)[index]
            assert pending["password"] == (index == 0)
            reply_to: dict[str, Any] = {
                "notebook_id": notebook_id,
                "input_id": pending["id"],
                "value": value,
            }
            reply = await call(
                app,
                "runtime.input",
                idempotency_key=f"answer-{index}",
                **reply_to,
            )
            assert reply.ok, reply.problem
            assert reply.data == {**pending, "status": "answered"}
            retried = await call(
                app,
                "runtime.input",
                idempotency_key=f"answer-{index}",
                **reply_to,
            )
            assert retried == reply
            duplicate = await call(
                app,
                "runtime.input",
                idempotency_key=f"duplicate-{index}",
                **reply_to,
            )
            assert duplicate.problem["type"].endswith(":conflict"), duplicate
        await wait_for(events, ended)
        [run] = [
            record["data"]["run"]
            for record in events.records
            if record.get("event") == "run"
            and record["data"]["run"]["status"] == "completed"
        ]
        assert run["outcome"] == "succeeded"
        inputs = [
            record
            for record in events.records
            if record.get("event") == "input"
        ]
        assert "secret" not in json.dumps(inputs)
        assert [record["data"]["request_id"] for record in inputs] == [
            "run",
            "answer-0",
            "run",
            "answer-1",
        ]


async def test_interrupt_closes_prompt_and_rejects_late_reply(
    tmp_path: Path,
) -> None:
    async with open_host(tmp_path, "import marimo\napp = marimo.App()\n") as (
        app,
        notebook_id,
        events,
    ):
        session_id = next(iter(app.state.session_manager.sessions))
        accepted = await call(
            app,
            "runtime.execute",
            headers={**AUTH, "Marimo-Session-Id": session_id},
            idempotency_key="run",
            notebook_id=notebook_id,
            code="input()",
        )
        assert accepted.ok, accepted.problem
        await wait_for(events, lambda response: "input" in response.names())
        pending = next(
            record["data"]["input"]
            for record in events.records
            if record.get("event") == "input"
        )
        cancelled = await call(
            app,
            "runtime.cancel",
            idempotency_key="interrupt",
            notebook_id=notebook_id,
            run_id=accepted.data["id"],
        )
        assert cancelled.ok, cancelled.problem
        late = await call(
            app,
            "runtime.input",
            idempotency_key="late",
            notebook_id=notebook_id,
            input_id=pending["id"],
            value="late",
        )
        assert late.problem["type"].endswith(":conflict"), late
        await wait_for(
            events,
            lambda response: any(
                record.get("event") == "run"
                and record["data"]["run"]["status"] == "completed"
                for record in response.records
            ),
        )
        [closed] = [
            record["data"]
            for record in events.records
            if record.get("event") == "input"
            and record["data"]["input"]["status"] == "cancelled"
        ]
        assert closed["request_id"] == "interrupt"
