# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import json
from pathlib import Path

from marimo._server.host.tools import contract

ROOT = Path(__file__).parents[3]


def test_the_committed_contract_matches_the_server() -> None:
    # Regenerate with `make fe-codegen`.
    committed = json.loads(
        (ROOT / "protocols/host/holon-contract.json").read_text()
    )
    assert committed["schema"]["tools"] == [
        {
            key: tool[key]
            for key in ("name", "inputSchema", "outputSchema")
            if key in tool
        }
        for tool in json.loads(json.dumps(contract()))
    ]
