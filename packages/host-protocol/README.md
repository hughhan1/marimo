# Host protocol definitions

[`Host`](src/Host.ts) defines the [host protocol](../../protocols/host/spec.md)
with Effect: each operation is a `Tool` in `Host.Tools`, and the event streams
are an `HttpApi`. The same definitions give typed tool handlers and the
[OpenAPI document](../../protocols/host/README.md#get-the-openapi-document)
whose `x-tools` and schemas the Python and TypeScript generators read.

Start with [`Objects.ts`](src/Objects.ts), the table of objects a host
publishes. The events come from that table, and an
[experimental feature](../../protocols/host/experimental/README.md) appends
rows to it.

## Implement a host

```ts
import { Effect } from "effect";
import { Host } from "@marimo-team/host-protocol";

const tools = Host.Tools.toLayer({
  "notebook.edit": ({ notebook_id, changes, idempotency_key }) =>
    edit(notebook_id, changes, idempotency_key),
  // ...one handler per tool
});
```

Serve the tools over MCP at `/mcp` below the API's URL, for example with
`McpServer` from `effect/ai`, and the streams with `HttpApiBuilder`. A host
also provides a layer for `Host.HostToken`, which admits requests that carry
the host record's token.

For a client, use [`@marimo-team/client`](../client/README.md).

## Working on the package

```sh
pnpm --dir packages/host-protocol typecheck
pnpm --dir packages/host-protocol test
pnpm --dir packages/host-protocol lint
pnpm --dir packages/host-protocol build
```

After a change to the definitions,
[regenerate and check all outputs](../../protocols/host/README.md#change-a-contract).
