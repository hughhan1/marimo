/* Copyright 2026 Marimo. All rights reserved. */

import NodeFs from "node:fs";
import NodeUrl from "node:url";
import { vi } from "vitest";
import { type OperationId, operations } from "../operations.ts";
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

/**
 * A `fetch` that serves the host's tools as `answer` answers the operation's
 * route.
 *
 * Each tool call becomes the request the operation's route describes: its
 * ids fill the path, its key and precondition become headers, and the rest
 * is the body. The response becomes the tool's result: a success's JSON is
 * its structured content, and a failure's body is its problem. `requests`
 * records those requests in order.
 */
export function host(
  answer: (request: Request) => Response | Promise<Response>,
) {
  const requests: Request[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    if (!new URL(request.url).pathname.endsWith("/mcp")) {
      // A stream, which stays a request of its own.
      requests.push(request.clone());
      return answer(request);
    }
    const message = (await request.json()) as {
      id: unknown;
      method: string;
      params: { name: OperationId; arguments: Record<string, unknown> };
    };
    const reply = (result: object) =>
      new Response(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }), {
        headers: { "Content-Type": "application/json" },
      });
    if (message.method === "server/discover") {
      return reply(DISCOVER);
    }
    const { name, arguments: args } = message.params;
    const operation = operations[name];
    const rest = { ...args };
    const url = new URL(request.url);
    url.pathname =
      url.pathname.replace(/\/mcp$/, "") +
      operation.path.replace(/\{(\w+)\}/g, (_, id: string) => {
        const value = String(rest[id]);
        delete rest[id];
        return encodeURIComponent(value);
      });
    const headers = new Headers();
    const authorization = request.headers.get("Authorization");
    if (authorization !== null) {
      headers.set("Authorization", authorization);
    }
    for (const header of Object.keys(operation.headers)) {
      const argument = header.toLowerCase().replaceAll("-", "_");
      if (argument in rest) {
        headers.set(header, String(rest[argument]));
        delete rest[argument];
      }
    }
    const routed = new Request(url, {
      method: operation.method,
      headers,
      body: operation.body === null ? undefined : JSON.stringify(rest),
    });
    requests.push(routed.clone());
    const response = await answer(routed);
    const text = await response.text();
    return reply(
      response.ok
        ? {
            resultType: "complete",
            content: [{ type: "text", text: `${name}: ok` }],
            structuredContent: text === "" ? {} : JSON.parse(text),
          }
        : {
            resultType: "complete",
            isError: true,
            content: [{ type: "text", text }],
          },
    );
  });
  return { fetch, requests };
}
