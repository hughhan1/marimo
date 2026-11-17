# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import signal
from typing import TYPE_CHECKING, Any

import uvicorn

from marimo import _loggers

if TYPE_CHECKING:
    import socket

    from marimo._server.api.tools import MCPEndpoint

LOGGER = _loggers.marimo_logger()


def initialize_signals() -> None:
    from packaging import version

    # 0.29.0 changed how uvicorn handles signals
    #
    # https://github.com/encode/uvicorn/pull/1600
    #
    # In 0.29.0, uvicorn re-throws signals after quitting, which leads to
    # ungraceful shutdowns since we use a SIGINT to kill uvicorn in the
    # first place
    #
    # Must be called before a uvicorn server is started
    if version.parse(uvicorn.__version__) >= version.parse("0.29.0"):

        def noop(signum: int, frame: Any) -> None:
            del signum
            del frame

        signal.signal(signal.SIGINT, noop)


class Server(uvicorn.Server):
    """A uvicorn server that closes its MCP event streams as it stops.

    Each stream ends with its final frame before the server stops waiting
    for its connections, so a client reconnects at once rather than after
    its stream went quiet.
    """

    def __init__(
        self, config: uvicorn.Config, *, endpoint: MCPEndpoint
    ) -> None:
        super().__init__(config)
        self.endpoint = endpoint

    async def shutdown(
        self, sockets: list[socket.socket] | None = None
    ) -> None:
        self.endpoint.close()
        await super().shutdown(sockets)


def close_uvicorn(server: uvicorn.Server) -> None:
    LOGGER.debug("Shutting down uvicorn")

    # Tried using sys.exit(0) to quit instead, but that ends up not being
    # graceful due to the event loop still running
    server.handle_exit(signal.SIGINT, None)

    LOGGER.debug("Shut down uvicorn")
