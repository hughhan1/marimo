/* Copyright 2026 Marimo. All rights reserved. */

import { type NotebookTransport, StreamStopped } from "@marimo-team/client";
import type { EventArguments, EventData } from "#holon-ui";
import { jsonParseWithSpecialChar } from "@/utils/json/json-parser";
import { classifyCloseEvent } from "./connection-state";
import {
  type StreamSlots,
  sharesConnections,
  streamSlots,
  unlimited,
} from "./stream-slots";

// The draft's Forbidden: the caller may not follow the session.
const FORBIDDEN = -32024;

/**
 * The error that ends a stream on this failure, or none to retry.
 *
 * A terminated subscription carries the reason the session would have
 * closed its socket with. `cause` is the connection status the editor shows.
 */
function stopOnFailure(error: unknown) {
  const { code, data } = (error ?? {}) as {
    code?: unknown;
    data?: { reason?: string };
  };
  const reason = code === FORBIDDEN ? "MARIMO_UNAUTHORIZED" : data?.reason;
  const decision = classifyCloseEvent({ reason });
  if (decision.kind === "retry") {
    return undefined;
  }
  const message = "reason" in decision.status ? decision.status.reason : "";
  return new StreamStopped(message, { cause: decision.status });
}

/** Send each request with the runtime's headers, read before each request. */
export function withHeaders(
  headers: () => Record<string, string>,
): typeof globalThis.fetch {
  return (input, init) => {
    const sent = new Headers(init?.headers);
    for (const [name, value] of Object.entries(headers())) {
      sent.set(name, value);
    }
    return globalThis.fetch(input, { ...init, headers: sent });
  };
}

/** What a page subscribes to its session with, as the server declares it. */
export type SessionArguments = EventArguments["marimo"]["session"];

/** Subscribe to the server's `session` event. */
export type SubscribeSession = (
  args: SessionArguments,
  options: { signal: AbortSignal },
) => Promise<AsyncIterable<{ data: EventData["marimo"]["session"] }>>;

let shared: StreamSlots | undefined;

/** This page's slots: limited where the browser shares HTTP/1.1 connections. */
function pageSlots(): StreamSlots {
  shared ??= sharesConnections() ? streamSlots() : unlimited;
  return shared;
}

/**
 * Follow a marimo session through its `session` MCP event.
 *
 * Subscribing connects to the session, as opening its socket did. Each
 * subscription starts the page over with a reset and a snapshot, so the
 * cursor goes unused. An event carries one frame's text, read by marimo's
 * parser, which reads `NaN`; a frame it cannot read arrives as an empty
 * object, which `NotebookEvents` rejects as not an event.
 */
export function sessionEvents(
  args: () => Promise<SessionArguments>,
  subscribe: SubscribeSession,
  slots: StreamSlots = pageSlots(),
): NotebookTransport {
  return async ({ signal }, { open, message }) => {
    let held: AbortSignal | undefined;
    try {
      held = await slots.hold(signal);
      const events = await subscribe(await args(), { signal: held });
      open();
      for await (const { data } of events) {
        message(jsonParseWithSpecialChar(data.frame));
      }
    } catch (error) {
      if (error instanceof StreamStopped) {
        throw error;
      }
      // A stream another tab needs follows the session again once it may.
      const stopped =
        signal.aborted || held?.aborted ? undefined : stopOnFailure(error);
      if (stopped !== undefined) {
        throw stopped;
      }
    }
  };
}
