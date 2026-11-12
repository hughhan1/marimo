# Copyright 2026 Marimo. All rights reserved.
"""Check scratch ownership and cleanup without a running host."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import scratch
from contract import Frame, Host


@asynccontextmanager
async def scratch_probe(*, root="/tmp", **overrides: Any):
    config = SimpleNamespace(stash=pytest.Stash())
    project = {
        "id": "p1",
        "name": "scratch",
        "root": root,
        "status": "indexed",
        "message": None,
        "operations": ["notebook.create"],
    }
    existing = {
        "id": "existing",
        "project_id": "p1",
        "path": "existing.py",
        "name": "existing.py",
        "operations": ["notebook.delete"],
        "updated_at": None,
    }
    catalog = [
        Frame(event=kind, data=json.dumps({kind: value}))
        for kind, value in (
            ("host", {"operations": ["notebook.create", "notebook.delete"]}),
            ("project", project),
            ("notebook", existing),
        )
    ]
    requests = []

    def respond(request):
        assert request.url.path == "/mcp"
        call = json.loads(request.content)["params"]
        name, arguments = call["name"], call["arguments"]
        requests.append((name, arguments.get("notebook_id")))
        reply: dict[str, Any] = {}
        if name == "notebook.create":
            reply = {
                **existing,
                "id": "new",
                "path": arguments.get("path"),
                "name": arguments.get("name", arguments.get("path")),
                **overrides,
            }
        else:
            assert name == "notebook.delete"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [], "structuredContent": reply},
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1/", transport=httpx.MockTransport(respond)
    ) as client:
        host = Host(client, timeout=1)

        def create():
            return scratch.__wrapped__(host, catalog, config)

        yield create, requests


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": "wrong"},
        {"path": "wrong.py"},
        {"root": None, "name": "wrong.py"},
    ],
)
async def test_invalid_create_metadata_is_cleaned_up(overrides):
    async with scratch_probe(**overrides) as (
        create,
        requests,
    ):
        with pytest.raises(AssertionError):
            await anext(create())
        assert requests == [
            ("notebook.create", None),
            ("notebook.delete", "new"),
        ]


async def test_existing_notebook_is_never_deleted():
    async with scratch_probe(id="existing") as (
        create,
        requests,
    ):
        with pytest.raises(AssertionError, match="existing notebook"):
            await anext(create())
        assert requests == [("notebook.create", None)]


async def test_undeletable_notebook_skips_remaining_scratch_checks():
    async with scratch_probe(
        operations=["notebook.edit", "runtime.start"],
    ) as (create, requests):
        for _ in range(2):
            with pytest.raises(
                pytest.skip.Exception, match=r"'new'.*notebook\.delete"
            ):
                await anext(create())
        assert requests == [("notebook.create", None)]


async def test_scratch_is_deleted_after_use():
    async with scratch_probe() as (create, requests):
        fixture = create()
        assert (await anext(fixture))["id"] == "new"
        assert requests == [("notebook.create", None)]
        await fixture.aclose()
        assert requests == [
            ("notebook.create", None),
            ("notebook.delete", "new"),
        ]
