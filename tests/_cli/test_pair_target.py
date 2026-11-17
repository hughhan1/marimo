# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import pytest

from marimo._cli.pair import target
from marimo._cli.pair.client import (
    AmbiguousNotebookError,
    HostEndpoint,
    NoNotebookError,
    PairError,
)
from marimo._cli.pair.discovery import Listing, RunningNotebook
from marimo._cli.pair.target import ExecutionTarget

HOST = HostEndpoint("http://127.0.0.1:2718/base/api/marimo/v1", "local-secret")
NOTEBOOK = RunningNotebook(
    "nb-one",
    "analysis",
    "/work/analysis.py",
    "http://127.0.0.1:2718/base",
    "running",
)


@pytest.mark.parametrize("credential", [None, "explicit-secret"])
def test_notebook_id_resolves_without_a_url(
    monkeypatch: pytest.MonkeyPatch,
    credential: str | None,
) -> None:
    monkeypatch.setattr(
        target,
        "list_running_notebooks",
        lambda _hosts=None: Listing([NOTEBOOK], []),
    )
    monkeypatch.setattr(target, "resolve_host", lambda _: HOST)
    result = target.resolve_target(
        url=None, session_id=NOTEBOOK.id, token=credential
    )
    assert result == ExecutionTarget(
        NOTEBOOK.url, NOTEBOOK.id, credential or HOST.token
    )
    assert "secret" not in repr(result)


@pytest.mark.parametrize("identifier", ["nb-one", "sess-one"])
def test_explicit_credentials_scope_discovery_to_the_requested_server(
    monkeypatch: pytest.MonkeyPatch,
    identifier: str,
) -> None:
    calls: list[dict[str, str | None]] = []

    def sessions(**kwargs: str | None) -> dict[str, dict[str, str]]:
        calls.append(kwargs)
        return {"browser": {"session_id": "sess-one", "notebook_id": "nb-one"}}

    monkeypatch.setattr(target, "list_sessions", sessions)
    monkeypatch.setattr(
        target,
        "find_host",
        lambda _: pytest.fail(
            "Explicit credentials must not use local authentication"
        ),
    )
    assert target.resolve_target(
        url="https://remote", session_id=identifier, token="explicit"
    ) == ExecutionTarget("https://remote", "nb-one", "explicit")
    assert calls == [{"url": "https://remote", "token": "explicit"}]


def test_local_url_uses_catalog_without_editor_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(target, "find_host", lambda _: HOST)
    monkeypatch.setattr(
        target,
        "list_running_notebooks",
        lambda _hosts: Listing([NOTEBOOK], []),
    )
    assert target.resolve_target(
        url=NOTEBOOK.url, session_id=None, token=None
    ) == ExecutionTarget(NOTEBOOK.url, NOTEBOOK.id, HOST.token)


@pytest.mark.parametrize(
    "identifier",
    [
        "sess-one",
        "session-1",
        "92a5d2d7-74b1-42f7-9b5d-540bd1a610db",
        "nb-opaque",
    ],
)
def test_explicit_local_session_is_found_in_the_servers_sessions(
    monkeypatch: pytest.MonkeyPatch,
    identifier: str,
) -> None:
    calls: list[dict[str, str | None]] = []

    def sessions(**kwargs: str | None) -> dict[str, dict[str, str]]:
        calls.append(kwargs)
        return {"browser": {"session_id": identifier, "notebook_id": "nb-two"}}

    monkeypatch.setattr(target, "find_host", lambda _: HOST)
    monkeypatch.setattr(
        target, "list_running_notebooks", lambda _: Listing([NOTEBOOK], [])
    )
    monkeypatch.setattr(target, "list_sessions", sessions)
    assert target.resolve_target(
        url=NOTEBOOK.url, session_id=identifier, token=None
    ) == ExecutionTarget(NOTEBOOK.url, "nb-two", HOST.token)
    assert calls == [{"url": NOTEBOOK.url, "token": HOST.token}]


def test_a_session_without_a_notebook_cannot_run_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        target,
        "list_sessions",
        lambda **_: {
            "browser": {"session_id": "sess-one", "notebook_id": None}
        },
    )
    with pytest.raises(NoNotebookError):
        target.resolve_target(
            url="https://remote", session_id="sess-one", token="explicit"
        )


def test_multiple_notebooks_require_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(target, "find_host", lambda _: HOST)
    monkeypatch.setattr(
        target,
        "list_running_notebooks",
        lambda _hosts: Listing(
            [
                NOTEBOOK,
                RunningNotebook(
                    "nb-two",
                    NOTEBOOK.name,
                    NOTEBOOK.path,
                    NOTEBOOK.url,
                    "running",
                ),
            ],
            [],
        ),
    )
    with pytest.raises(AmbiguousNotebookError) as error:
        target.resolve_target(url=NOTEBOOK.url, session_id=None, token=None)
    assert error.value.candidates == ("nb-one", "nb-two")


def test_unknown_id_does_not_select_a_different_notebook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        target,
        "list_running_notebooks",
        lambda _hosts=None: Listing([NOTEBOOK], []),
    )
    with pytest.raises(PairError, match="No running notebook matches"):
        target.resolve_target(url=None, session_id="nb-old", token=None)


def test_remote_url_keeps_legacy_session_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        target,
        "list_sessions",
        lambda **_kwargs: {
            "browser": {"session_id": "sess-one", "notebook_id": "nb-one"},
        },
    )
    assert target.resolve_target(
        url="https://remote", session_id=None, token="explicit"
    ) == ExecutionTarget("https://remote", "nb-one", "explicit")


@pytest.mark.parametrize("known", [True, False])
def test_url_restricts_notebook_discovery_to_one_local_host(
    monkeypatch: pytest.MonkeyPatch,
    known: bool,
) -> None:
    seen: list[list[HostEndpoint] | None] = []

    def listing(hosts: list[HostEndpoint] | None = None) -> Listing:
        seen.append(hosts)
        assert hosts == [HOST]
        return Listing([NOTEBOOK] if known else [], [])

    monkeypatch.setattr(target, "find_host", lambda _: HOST)
    monkeypatch.setattr(target, "list_running_notebooks", listing)
    if known:
        assert target.resolve_target(
            url=NOTEBOOK.url,
            notebook_id=NOTEBOOK.id,
            session_id=None,
            token=None,
        ) == ExecutionTarget(NOTEBOOK.url, NOTEBOOK.id, HOST.token)
    else:
        with pytest.raises(PairError, match="No running notebook matches"):
            target.resolve_target(
                url=NOTEBOOK.url,
                notebook_id=NOTEBOOK.id,
                session_id=None,
                token=None,
            )
    assert seen == [[HOST]]


def test_remote_scope_does_not_substitute_an_unrelated_notebook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        target,
        "list_sessions",
        lambda **_: {
            "browser": {"session_id": "sess-other", "notebook_id": "nb-other"},
        },
    )
    with pytest.raises(PairError, match="No running notebook matches"):
        target.resolve_target(
            url="https://remote", session_id="nb-missing", token="explicit"
        )


@pytest.mark.parametrize("selector", ["session_id", "notebook_id"])
@pytest.mark.parametrize("identifier", ["sess-not-a-session", "opaque-id"])
def test_catalog_membership_identifies_notebooks(
    monkeypatch: pytest.MonkeyPatch, selector: str, identifier: str
) -> None:
    notebook = RunningNotebook(
        identifier, NOTEBOOK.name, NOTEBOOK.path, NOTEBOOK.url, "running"
    )
    monkeypatch.setattr(target, "find_host", lambda _: HOST)
    monkeypatch.setattr(
        target, "list_running_notebooks", lambda _: Listing([notebook], [])
    )
    assert target.resolve_target(
        url=NOTEBOOK.url,
        session_id=identifier if selector == "session_id" else None,
        notebook_id=identifier if selector == "notebook_id" else None,
        token=None,
    ) == ExecutionTarget(NOTEBOOK.url, identifier, HOST.token)


def test_local_session_resolution_does_not_ignore_discovery_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(target, "find_host", lambda _: HOST)
    monkeypatch.setattr(
        target,
        "list_running_notebooks",
        lambda _: Listing([], ["Could not read the catalog."]),
    )
    with pytest.raises(PairError, match="Could not read the catalog"):
        target.resolve_target(
            url=NOTEBOOK.url, session_id="opaque-id", token=None
        )
