# Copyright 2026 Marimo. All rights reserved.
"""Configure a disposable host through its discovery record."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from contract import Failure, Host, failed, objects, validate

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_SCRATCH_UNAVAILABLE = pytest.StashKey[str]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--record", help="Path to a running host's discovery record"
    )
    parser.addoption(
        "--host-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for host responses and events (default: %(default)s)",
    )
    parser.addoption(
        "--require-operation",
        action="append",
        default=[],
        help=(
            "Require an operation instead of skipping when unavailable. "
            "Repeat for multiple operations"
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--require-operation") and not config.getoption(
        "--record"
    ):
        raise pytest.UsageError("--require-operation needs --record")


@pytest.fixture(scope="session")
def record(pytestconfig: pytest.Config) -> dict[str, Any]:
    import psutil

    filename = pytestconfig.getoption("--record")
    if filename is None:
        pytest.skip("pass --record to check a running host")
    path = Path(filename)
    if os.name == "posix":
        for resource in (path, path.parent):
            stat = resource.stat()
            assert stat.st_uid == os.getuid(), (
                "record and directory must belong to this user"
            )
            assert stat.st_mode & 0o077 == 0, (
                "record and directory must be private"
            )
    value = json.loads(path.read_text(encoding="utf-8"))
    validate("HostRecord", value)
    url = urlsplit(value["url"])
    assert url.scheme == "http"
    assert url.hostname in ("127.0.0.1", "::1")
    assert url.username is None
    assert url.password is None
    assert not url.query
    assert not url.fragment
    process = psutil.Process(value["process"]["pid"])
    started = datetime.fromisoformat(value["process"]["started_at"])
    assert abs(process.create_time() - started.timestamp()) < 1, (
        "stale process identity"
    )
    return value


@pytest.fixture
async def host(
    record: dict[str, Any], pytestconfig: pytest.Config
) -> AsyncIterator[Host]:
    timeout = pytestconfig.getoption("--host-timeout")
    assert timeout > 0
    async with httpx.AsyncClient(
        base_url=record["url"].rstrip("/") + "/",
        headers={"Authorization": f"Bearer {record['token']}"},
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        yield Host(client, timeout)


@pytest.fixture
async def catalog(host: Host) -> list:
    return await host.snapshot("catalog")


@pytest.fixture
async def scratch(
    host: Host, catalog: list, pytestconfig: pytest.Config
) -> AsyncIterator[dict[str, Any]]:
    if _SCRATCH_UNAVAILABLE in pytestconfig.stash:
        pytest.skip(pytestconfig.stash[_SCRATCH_UNAVAILABLE])
    projects = objects(catalog, "project")
    available = set(objects(catalog, "host")[0]["operations"])
    if not {"notebook.create", "notebook.delete"} <= available:
        pytest.skip(
            "scratch checks require notebook.create and notebook.delete"
        )
    project = next(
        (p for p in projects if "notebook.create" in p["operations"]), None
    )
    if project is None:
        pytest.skip("no project permits notebook.create")
    name = f"conformance-{uuid4().hex}.py"
    notebook = await host.call(
        "notebook.create",
        project_id=project["id"],
        **{"path" if project["root"] else "name": name},
    )
    existing = {n["id"] for n in objects(catalog, "notebook")}
    assert notebook["id"] not in existing, (
        "create claimed an existing notebook"
    )
    if "notebook.delete" not in notebook["operations"]:
        # Permissions are only known after creation. Avoid creating more
        # notebooks that this client cannot clean up during this session.
        reason = (
            f"scratch notebook {notebook['id']!r} does not permit "
            "notebook.delete; remove it from the disposable workspace manually"
        )
        pytestconfig.stash[_SCRATCH_UNAVAILABLE] = reason
        pytest.skip(reason)
    try:
        assert notebook["project_id"] == project["id"]
        assert (
            notebook["path"] == name
            if project["root"]
            else notebook["name"] == name
        )
        yield notebook
    finally:
        if "runtime.stop" in notebook["operations"]:
            stopped = await host.call(
                "runtime.stop", notebook_id=notebook["id"]
            )
            if isinstance(stopped, Failure):
                failed(stopped, "no-runtime")
            else:
                async with host.subscribe(
                    "notebook", notebook_id=notebook["id"]
                ) as events:
                    snapshot = await events.ready()
                    if objects(snapshot, "runtime"):
                        await events.until(
                            lambda e: e.event == "runtime.removed"
                        )
        deleted = await host.call(
            "notebook.delete", notebook_id=notebook["id"]
        )
        if isinstance(deleted, Failure):
            failed(deleted, "not-found")
