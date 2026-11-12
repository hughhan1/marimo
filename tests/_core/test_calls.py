# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from typing import Any

import msgspec
import pytest

from marimo._core import protocol
from marimo._core.calls import decode

NOTEBOOK = protocol.NotebookId("n1")
KEYED = {"idempotency_key": "k", "notebook_id": "n1"}


def test_a_call_is_its_tools_arguments() -> None:
    assert decode("runtime.cancel", {**KEYED, "run_id": "r1"}) == (
        protocol.CancelRunRequest(
            idempotency_key="k",
            notebook_id=NOTEBOOK,
            run_id=protocol.RunId("r1"),
        )
    )
    assert decode("runtime.interrupt", KEYED) == protocol.NotebookRequest(
        idempotency_key="k", notebook_id=NOTEBOOK
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("notebook.missing", {}),
        ("runtime.cancel", {}),
        ("runtime.interrupt", {"authorization": "Bearer t"}),
        ("notebook.save", {"if_match": '"a"'}),
        # Each execution tool refuses the other's payload.
        ("runtime.run", {"code": "1"}),
        ("runtime.run", {"cells": []}),
        ("runtime.run", {"cells": [{"id": "c1"}], "code": "1"}),
        ("runtime.execute", {"cells": [{"id": "c1"}]}),
        ("runtime.execute", {"code": "1", "cells": [{"id": "c1"}]}),
    ],
)
def test_a_call_takes_only_its_tools_arguments(
    name: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(msgspec.ValidationError):
        decode(name, {**KEYED, **arguments})
