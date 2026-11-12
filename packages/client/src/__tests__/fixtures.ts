/* Copyright 2026 Marimo. All rights reserved. */

import NodeFs from "node:fs";
import NodeUrl from "node:url";
import { vi } from "vitest";
import * as Protocol from "../protocol.ts";
import type { Transport } from "../transport/frame.ts";

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

/**
 * A transport whose connections each deliver the next list of messages and
 * end. The last one stays open until the stream closes. `attempts` records the
 * cursor each connection started from.
 */
export function scripted(...connections: (readonly unknown[])[]) {
  const attempts: (string | null)[] = [];
  const transport: Transport = async (
    { cursor, signal },
    { open, message },
  ) => {
    attempts.push(cursor);
    open();
    connections.shift()?.forEach((each) => message(each));
    if (connections.length === 0 && !signal.aborted) {
      await new Promise<void>((resolve) =>
        signal.addEventListener("abort", () => resolve(), { once: true }),
      );
    }
  };
  return { transport, attempts };
}

/** The host's answer to `server/discover`, as marimo's sends it. */
const DISCOVER = {
  supportedVersions: ["2026-07-28"],
  capabilities: { tools: { listChanged: false } },
  ttlMs: 0,
  cacheScope: "private",
  _meta: {
    "io.modelcontextprotocol/serverInfo": { name: "marimo", version: "0" },
  },
};

/** A tool call or subscription a host received, with its `Authorization`. */
export interface Received {
  readonly name: string;
  readonly arguments: Record<string, unknown>;
  readonly authorization: string | null;
}

/**
 * How a host answers a subscription: it refuses it with a problem, or sends
 * protocol events and keeps the connection open, or loses it after them.
 */
export type Subscription =
  | { readonly problem: unknown }
  | { readonly events: readonly unknown[]; readonly lose?: true };

/** One JSON-RPC notification as an SSE frame. */
function notification(method: string, params: object): string {
  const message = { jsonrpc: "2.0", method: `notifications/events/${method}` };
  return `data: ${JSON.stringify({ ...message, params })}\n\n`;
}

/**
 * A `fetch` that serves a host's tools and events at its `/mcp`.
 *
 * `answer` answers each tool call with its result, or `{ problem }` to fail
 * it, and `subscribe` answers each subscription. `calls` and `subscriptions`
 * record them in order.
 */
export function host(
  answer: (call: Received) => unknown,
  subscribe: (subscription: Received) => Subscription = () => ({ events: [] }),
) {
  const calls: Received[] = [];
  const subscriptions: Received[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    const message = (await request.json()) as {
      id?: unknown;
      method: string;
      params: { name: string; arguments: Record<string, unknown> };
    };
    const reply = (result: object) =>
      Response.json({
        jsonrpc: "2.0",
        id: message.id,
        result: { resultType: "complete", ...result },
      });
    const received = {
      name: message.params?.name,
      arguments: message.params?.arguments,
      authorization: request.headers.get("Authorization"),
    };
    switch (message.method) {
      case "server/discover":
        return reply(DISCOVER);
      case "tools/call": {
        calls.push(received);
        const answered = (await answer(received)) as
          | { problem?: unknown }
          | undefined;
        if (answered !== undefined && "problem" in answered) {
          return reply({
            isError: true,
            content: [{ type: "text", text: JSON.stringify(answered.problem) }],
          });
        }
        return reply({
          content: [{ type: "text", text: `${received.name}: ok` }],
          structuredContent: answered ?? {},
        });
      }
      case "events/list":
        return reply({
          events: ["catalog", "notebook"].map((name) => ({
            name,
            delivery: ["push"],
          })),
        });
      case "events/stream": {
        subscriptions.push(received);
        const answered = subscribe(received);
        if ("problem" in answered) {
          return Response.json({
            jsonrpc: "2.0",
            id: message.id,
            error: {
              code: -32001,
              message: "Refused",
              data: { problem: answered.problem },
            },
          });
        }
        const frames = [
          notification("active", { cursor: null }),
          ...answered.events.map((data, index) =>
            notification("event", {
              name: received.name,
              eventId: String(index),
              timestamp: "2026-11-12T00:00:00Z",
              data,
              cursor: null,
            }),
          ),
        ];
        const body = new ReadableStream<string>({
          start(controller) {
            frames.forEach((each) => controller.enqueue(each));
            if (answered.lose) {
              controller.close();
            } else {
              request.signal.addEventListener("abort", () =>
                controller.error(request.signal.reason),
              );
            }
          },
        });
        return new Response(body.pipeThrough(new TextEncoderStream()), {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      default:
        // A notification, which has no answer.
        return new Response(null, { status: 202 });
    }
  });
  return { fetch, calls, subscriptions };
}
