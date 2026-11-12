# Copyright 2026 Marimo. All rights reserved.
"""Server-sent events (SSE) framing and connection utilities.

Shared by the endpoints that stream `text/event-stream` responses (the
scratchpad `/api/kernel/execute` stream and the host API's event streams).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    # Disable proxy buffering (nginx) so events are delivered immediately
    "X-Accel-Buffering": "no",
}


async def wait_for_http_disconnect(request: Request) -> None:
    """Wait until the client disconnects.

    `request._receive` is the ASGI `receive` callable; reading it is the
    standard way to detect disconnects. Note that on ASGI servers
    advertising spec_version < 2.4 (e.g. uvicorn), Starlette's
    `StreamingResponse` also listens on the same receive channel and
    cancels the response on disconnect — so callers must not rely on this
    helper as their only cleanup path; treat it as a prompt-shutdown
    signal and put teardown in a `finally` that also runs on cancellation.
    """
    while True:
        message = await request._receive()
        if message.get("type") == "http.disconnect":
            return
