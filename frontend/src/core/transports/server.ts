/* Copyright 2026 Marimo. All rights reserved. */

import { type NotebookTransport, StreamStopped } from "@marimo-team/client";
import type { EventArguments, EventData } from "#holon-ui";
import { z } from "zod";
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
  const failure = z
    .object({
      code: z.number().optional(),
      data: z
        .object({ reason: z.string().optional() })
        .optional()
        .catch(undefined),
    })
    .safeParse(error);
  const reason = failure.success
    ? failure.data.code === FORBIDDEN
      ? "MARIMO_UNAUTHORIZED"
      : failure.data.data?.reason
    : undefined;
  const decision = classifyCloseEvent({ reason });
  if (decision.kind === "retry") {
    return undefined;
  }
  const message = "reason" in decision.status ? decision.status.reason : "";
  return new StreamStopped(message, { cause: decision.status });
}

/** What a page subscribes to its session with, as the server declares it. */
export type SessionArguments = EventArguments["marimo"]["session"];

/** Follow the server's `session` event until cancelled or disconnected. */
export type WatchSession = (
  args: SessionArguments,
  onEvent: (event: { data: EventData["marimo"]["session"] }) => void,
  options: { signal: AbortSignal; onOpen: () => void },
) => Promise<void>;

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
 * cursor goes unused.
 */
export function sessionEvents(
  args: () => Promise<SessionArguments>,
  watch: WatchSession,
  slots: StreamSlots = pageSlots(),
): NotebookTransport {
  return async ({ signal }, { open, message }) => {
    const attempt = new AbortController();
    const active = AbortSignal.any([signal, attempt.signal]);
    let held: AbortSignal | undefined;
    try {
      held = await slots.hold(active);
      await watch(await args(), ({ data }) => message(JSON.parse(data.frame)), {
        signal: held,
        onOpen: open,
      });
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
    } finally {
      attempt.abort();
    }
  };
}
