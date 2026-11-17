# Copyright 2026 Marimo. All rights reserved.
"""The host protocol's tools and their handlers."""

from __future__ import annotations

from marimo._core.calls import HANDLERS
from marimo._core.protocol_tools import TOOLS
from marimo._server.host.api import CATALOG


def test_every_tool_has_one_handler() -> None:
    assert HANDLERS.keys() | CATALOG.keys() == TOOLS.keys()
    assert not HANDLERS.keys() & CATALOG.keys()
