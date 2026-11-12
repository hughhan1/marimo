# Copyright 2026 Marimo. All rights reserved.
"""Send a WASM notebook calls written in the protocol's parts.

The notebook takes a call as its tool's arguments, flat, and replies
`{ok, data}` or `{ok, problem}`. `flat` turns `{operation, params, headers,
body}` into that call, and `answered` turns the reply into `{status, body}`:
the status the operation's route would answer with, and its reply or problem.
"""

from __future__ import annotations

from typing import Any

from tests._server.host.routes import success_status


def flat(call: dict[str, Any]) -> dict[str, Any]:
    """The call's arguments: its params, its headers by Python name, its body's members."""
    arguments: dict[str, Any] = {
        "operation": call["operation"],
        **call.get("params", {}),
    }
    for header, value in call.get("headers", {}).items():
        arguments[header.lower().replace("-", "_")] = value
    body = call.get("body")
    if isinstance(body, dict):
        arguments.update(body)
    elif body is not None:
        # Not an object: sent as a member no call declares, to be refused.
        arguments["body"] = body
    return arguments


def answered(operation: str, reply: dict[str, Any]) -> dict[str, Any]:
    """The reply as `{status, body}`."""
    if not reply["ok"]:
        return {"status": reply["problem"]["status"], "body": reply["problem"]}
    data = reply["data"]
    status = success_status(operation, data)
    return {"status": status, "body": None if status == 204 else data}
