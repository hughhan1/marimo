# Host protocol conformance tests

From the repository root, check the Python bindings and portable fixtures
with:

```sh
make host-protocol-check
```

The suite validates against the OpenAPI document, which it asks the
[host protocol package](../README.md#get-the-openapi-document) to print. That
needs Node and the package's one dependency. The document's `paths` are the
event streams, and its `x-tools` lists the tools: each one's arguments,
result and the problems it may fail with.

```sh
pnpm install --filter @marimo-team/host-protocol --prod
```

This includes experimental fixtures. Tests that need a running host are
skipped. The [contributor workflow](../README.md#change-a-contract) also
covers TypeScript generation and package checks.

## Check a running host

Start a host in a disposable workspace and pass its
[discovery record](../spec.md#local-discovery). The suite calls the host's
tools over MCP at `/mcp` below the record's URL, and reads its event streams
over HTTP. It checks each result against the tool's `outputSchema` and each
failure against the tool's `problems`. It creates and modifies notebooks and
starts and stops runtimes; you manage the host's lifecycle.

```sh
uv run --locked --project protocols/host/conformance \
  pytest -c protocols/host/conformance/pyproject.toml \
  protocols/host/conformance protocols/host/experimental \
  --record /path/to/hosts-v1/host.json
```

The suite skips tests for capabilities the host does not offer, so review the
skipped tests. Repeat `--require-operation <operation>` to assert advertised
operations, and use `--slow` to include idle ping checks. Replace `--record`
and its path with `--help` for all options.
