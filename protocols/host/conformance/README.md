# Host protocol conformance tests

From the repository root, check the Python bindings and portable fixtures
with:

```sh
make host-protocol-check
```

The suite validates against the OpenAPI document, which it asks the
[host protocol package](../README.md#get-the-openapi-document) to print. That
needs Node and the package's one dependency. The document's `x-tools` lists
the tools: each one's arguments, result and the problems it may fail with.
Its `x-events` lists the two event streams, `catalog` and `notebook`: each
one's arguments and the protocol events it carries.

```sh
pnpm install --filter @marimo-team/host-protocol --prod
```

This includes experimental fixtures. Tests that need a running host are
skipped. The [contributor workflow](../README.md#change-a-contract) also
covers TypeScript generation and package checks.

## Check a running host

Start a host in a disposable workspace and pass its
[discovery record](../spec.md#local-discovery). The suite calls the host's
tools and subscribes to its events over MCP at `/mcp` below the record's URL.
It checks each result against the tool's `outputSchema`, each failure against
the tool's `problems`, each protocol event against its event's
`payloadSchema`, and each refused subscription's problem against `Problem`.
It creates and modifies notebooks and starts and stops runtimes; you manage
the host's lifecycle.

```sh
uv run --locked --project protocols/host/conformance \
  pytest -c protocols/host/conformance/pyproject.toml \
  protocols/host/conformance protocols/host/experimental \
  --record /path/to/hosts-v1/host.json
```

The suite skips tests for capabilities the host does not offer, so review the
skipped tests. Repeat `--require-operation <operation>` to assert advertised
operations. Replace `--record` and its path with `--help` for all options.
