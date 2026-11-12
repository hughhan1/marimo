# Host protocol examples

JSON records and event sequences illustrate the [host protocol](../spec.md)
and serve as shared fixtures for clients and the [conformance suite](../conformance/README.md).
Each `.sse` file writes the events one subscription delivers in server-sent
event notation: `event` is the event's name, `id` its cursor, and `data` its
data. The examples use fictional data and include snapshots, live events, and
replay.

Run `make host-protocol-check` from the repository root to validate the fixtures.
