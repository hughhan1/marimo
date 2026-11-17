# Copyright 2026 Marimo. All rights reserved.
"""Shared state for the host API's tools and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from marimo._core.operations import Mutations

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

    from marimo._server.host.bridge import SessionBridge


@dataclass
class HostContext:
    """The session bridge and retry receipts."""

    bridge: SessionBridge
    mutations: Mutations = field(init=False)

    def __post_init__(self) -> None:
        self.mutations = Mutations(self.bridge.host)


def host_context(request: HTTPConnection) -> HostContext:
    """The host the editor serves while it runs."""
    context: HostContext | None = getattr(
        request.app.state, "host_context", None
    )
    assert context is not None, "The editor serves the host while it runs"
    return context
