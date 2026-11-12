# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import msgspec
import pytest
from inline_snapshot import snapshot

from marimo._cli.pair import discovery
from marimo._cli.pair.client import HostEndpoint
from marimo._core import protocol
from tests._core.conftest import project, record
from tests.mcp_request import stream_frame


def catalog(*, ready: bool = True) -> bytes:
    entries: list[tuple[str, object]] = [
        (
            "project",
            protocol.ProjectMessage(
                msgspec.structs.replace(project(), root="/work")
            ),
        ),
    ]
    for name, status in [
        ("live", "running"),
        ("closed", None),
        ("dead", "failed"),
        ("untitled", "running"),
    ]:
        notebook = msgspec.structs.replace(
            record(name),
            path=None if name == "untitled" else "analysis.py",
            name=name,
        )
        entries.append(("notebook", protocol.NotebookMessage(notebook)))
        if status is not None:
            runtime = protocol.Runtime(
                id=protocol.RuntimeId(f"rt-{name}"),
                notebook_id=notebook.id,
                status=status,
                marimo_version=None,
                message=None,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            entries.append(("runtime", protocol.RuntimeMessage(runtime)))
    if ready:
        entries.append(("ready", {}))
    return b"".join(
        stream_frame(
            "event",
            {"data": {"event": name, "data": msgspec.to_builtins(value)}},
        )
        for name, value in entries
    )


def test_listing_keeps_only_running_notebooks_and_closes_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.__iter__.side_effect = lambda: iter(
        catalog().splitlines(keepends=True)
    )
    subscribed: list[dict[str, object]] = []

    def open_events(**kwargs: object) -> MagicMock:
        subscribed.append(kwargs)
        return response

    monkeypatch.setattr(discovery, "open_events", open_events)
    result = discovery.list_running_notebooks(
        [HostEndpoint("http://127.0.0.1:2718/base/api/marimo/v1", "secret")]
    )
    assert msgspec.to_builtins(result) == snapshot(
        {
            "notebooks": [
                {
                    "id": "live",
                    "name": "live",
                    "path": str(Path("/work/analysis.py")),
                    "url": "http://127.0.0.1:2718/base",
                    "status": "running",
                },
                {
                    "id": "untitled",
                    "name": "untitled",
                    "path": None,
                    "url": "http://127.0.0.1:2718/base",
                    "status": "running",
                },
            ],
            "warnings": [],
        }
    )
    assert subscribed == [
        {
            "url": "http://127.0.0.1:2718/base/api/marimo/v1",
            "name": "catalog",
            "arguments": {"types": ["project", "notebook", "runtime"]},
            "token": "secret",
            "read_timeout": discovery._TIMEOUT,
        }
    ]
    response.close.assert_called_once()


@pytest.mark.parametrize("failure", ["incomplete", "malformed", "timeout"])
def test_listing_preserves_other_servers_when_a_snapshot_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def open_events(*, url: str, **_: object) -> MagicMock:
        response = MagicMock()
        data = catalog()
        if ":2719/" in url:
            if failure == "timeout":
                raise TimeoutError("secret diagnostic")
            data = (
                catalog(ready=False)
                if failure == "incomplete"
                else b'event: message\ndata: {"secret":}\n\n'
            )
        response.__iter__.return_value = iter(data.splitlines(keepends=True))
        return response

    monkeypatch.setattr(discovery, "open_events", open_events)
    result = discovery.list_running_notebooks(
        [
            HostEndpoint(f"http://127.0.0.1:{port}/api/marimo/v1", "secret")
            for port in [2718, 2719]
        ]
    )
    assert [n.id for n in result.notebooks] == ["live", "untitled"]
    assert result.warnings == [
        "Could not list notebooks on http://127.0.0.1:2719/api/marimo/v1."
    ]


def test_snapshot_deadline_includes_heartbeat_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([0.0, 1.0, 10.0])
    monkeypatch.setattr(discovery, "monotonic", lambda: next(ticks))
    heartbeat = stream_frame("heartbeat", {"cursor": "1"})
    lines = discovery._bounded_lines([heartbeat, heartbeat])
    assert next(lines) == heartbeat
    with pytest.raises(TimeoutError):
        next(lines)
