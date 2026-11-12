/* Copyright 2026 Marimo. All rights reserved. */

import NodeFs from "node:fs";
import NodeUrl from "node:url";
import { vi } from "vitest";
import * as Protocol from "../protocol.ts";

/** Read one of the protocol's portable SSE examples as decoded events. */
export function fixtureEvents(name: string): Protocol.EventOutput[] {
  const path = new NodeUrl.URL(
    `../../../../protocols/host/fixtures/${name}`,
    import.meta.url,
  );
  return NodeFs.readFileSync(path, "utf8")
    .split(/\r?\n\r?\n/)
    .flatMap((frame) => {
      const event = /^event: (.+)$/m.exec(frame)?.[1];
      if (!event) {
        return [];
      }
      const data = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      const id = /^id: (.+)$/m.exec(frame)?.[1];
      return [
        Protocol.Event.parse({
          event,
          data: JSON.parse(data),
          ...(id ? { id } : {}),
        }),
      ];
    });
}

/** The host's answer to `server/discover`, as marimo's sends it. */
const DISCOVER = {
  resultType: "complete",
  supportedVersions: ["2026-07-28"],
  capabilities: { tools: { listChanged: false } },
  ttlMs: 0,
  cacheScope: "private",
  _meta: {
    "io.modelcontextprotocol/serverInfo": { name: "marimo", version: "0" },
  },
};

/** A tool call a host received, with the `Authorization` it came with. */
export interface Received {
  readonly name: string;
  readonly arguments: Record<string, unknown>;
  readonly authorization: string | null;
}

/**
 * A `fetch` that serves a host's tools at its `/mcp`: `answer` answers each
 * call with its result, or `{ problem }` to fail it. Other requests, such as
 * streams, go to `stream`. `calls` records the tool calls in order.
 */
export function host(
  answer: (call: Received) => unknown,
  stream: (request: Request) => Response | Promise<Response> = () =>
    new Response(null, { status: 404 }),
) {
  const calls: Received[] = [];
  const requests: Request[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    if (!new URL(request.url).pathname.endsWith("/mcp")) {
      requests.push(request.clone());
      return stream(request);
    }
    const message = (await request.json()) as {
      id: unknown;
      method: string;
      params: { name: string; arguments: Record<string, unknown> };
    };
    const reply = (result: object) =>
      new Response(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }), {
        headers: { "Content-Type": "application/json" },
      });
    if (message.method === "server/discover") {
      return reply(DISCOVER);
    }
    const call = {
      name: message.params.name,
      arguments: message.params.arguments,
      authorization: request.headers.get("Authorization"),
    };
    calls.push(call);
    const answered = (await answer(call)) as { problem?: unknown } | undefined;
    if (answered !== undefined && "problem" in answered) {
      return reply({
        resultType: "complete",
        isError: true,
        content: [{ type: "text", text: JSON.stringify(answered.problem) }],
      });
    }
    return reply({
      resultType: "complete",
      content: [{ type: "text", text: `${call.name}: ok` }],
      structuredContent: answered ?? {},
    });
  });
  return { fetch, calls, requests };
}
