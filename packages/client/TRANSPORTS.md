# Custom transports

`Client` and `NotebookClient` call the host's MCP tools and follow its MCP
events. To follow the protocol's events from another source, such as a
WebSocket, a message channel, or a host running in the page, pass a
`Transport` function to `NotebookEvents` or `CatalogEvents`.

The stream calls the transport once for each connection attempt. The
transport receives a cursor and an abort signal. A null cursor requests a
snapshot. Otherwise, the transport sends the cursor to the host to resume
after that position.

Call `open()` when the connection is established. Call `message(event)` for
each decoded envelope, in order, with `event`, `data`, and an optional `id`.
The stream validates the protocol payload. Close the connection when the
signal is aborted.

Resolve when the connection ends and another attempt makes sense. Reject
when the stream should stop. The stream's `closed` promise rejects with the
same error. Use `StreamStopped` to describe the failure. Calls to `open()` or
`message(event)` after the promise settles are ignored.

The stream validates known events, tracks the cursor, delivers events to
subscribers, and reconnects. Malformed known events stop the stream. Unknown
event names retain their cursor and are ignored unless explicitly requested
through `types`; requested extension events carry unvalidated data.
It delivers `reset` when an interrupted snapshot must be discarded. The host
must also send `reset` when it cannot resume from the supplied cursor.

The transport sends any filters the host needs. The stream's `types` option
filters delivered events locally. A notebook transport must apply `runId`
itself. Each reconnect must use the same filters.

See the [transport contract](src/transport/frame.ts) for the function and
event types, and [the MCP transport](src/transport/mcp.ts) for one.
