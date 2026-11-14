/* Copyright 2026 Marimo. All rights reserved. */

import { type NotebookTransport, StreamStopped } from "@marimo-team/client";
import type { EventArguments, EventData, Outputs } from "#holon-ui";
import { closedStatus } from "./connection-state";

// The draft's Forbidden: the caller may not follow the session.
const FORBIDDEN = -32024;
const CONNECTION_CLOSED = -32000;
const REQUEST_TIMEOUT = -32001;

/** What a page subscribes to its session with, as the server declares it. */
export type SessionArguments = EventArguments["marimo"]["session"];

/** One change to a page's session. */
export type SessionEvent = EventData["marimo"]["session"];

/** A page's session as of a cursor of its `session` event. */
export type SessionState = Outputs["marimo"]["session_state"];

/** How a page reads its session, and follows it from a cursor. */
export interface Session {
  read(
    args: SessionArguments,
    options: { signal: AbortSignal },
  ): Promise<SessionState>;
  follow(
    args: SessionArguments,
    options: {
      signal: AbortSignal;
      cursor: string;
      delivery: "push" | "poll";
      onGap: () => void;
    },
  ): Promise<
    AsyncIterable<{ eventId: string; data: SessionEvent }> & {
      cursor: string | null;
    }
  >;
}

function stopped(reason: string | undefined) {
  const status = closedStatus(reason);
  return new StreamStopped(status.reason, { cause: status });
}

function reasonOf(error: unknown): string | undefined {
  const failure = error as { code?: unknown; data?: { reason?: unknown } };
  const reason = failure?.data?.reason;
  if (typeof reason === "string") {
    return reason;
  }
  return failure?.code === FORBIDDEN ? "MARIMO_UNAUTHORIZED" : undefined;
}

function answered(error: unknown): boolean {
  const failure = error as {
    code?: unknown;
    status?: unknown;
    result?: unknown;
  } | null;
  return (
    failure?.result !== undefined ||
    (typeof failure?.code === "number" &&
      failure.code !== CONNECTION_CLOSED &&
      failure.code !== REQUEST_TIMEOUT) ||
    (typeof failure?.status === "number" &&
      failure.status >= 400 &&
      failure.status < 500)
  );
}

/**
 * Follow a marimo session: read its state, then follow its `session` event
 * from the state's cursor.
 *
 * The client reconnects and resumes from the cursor by itself. The page in
 * the focused window is pushed each change and every other page polls, so
 * of a browser's pages only one holds a connection open. When the server
 * skipped changes, the page reads its state again. A page that switches
 * between push and poll, or reads its state again, keeps its subscription
 * until the next one is confirmed, so it stays in its session throughout.
 * A failure the server answered ends the transport for good; any other
 * failure to read or subscribe ends the attempt, which is made again.
 */
export function sessionEvents(
  args: () => Promise<SessionArguments>,
  session: Session,
  view: Pick<
    Window,
    "document" | "addEventListener" | "removeEventListener"
  > = globalThis.window,
): NotebookTransport {
  const delivery = () => (view.document.hasFocus() ? "push" : "poll");
  return async ({ signal }, { open, message }) => {
    const named = await args();
    let held: { turn: AbortController; drained: Promise<unknown> } | undefined;
    try {
      while (!signal.aborted) {
        let state: SessionState;
        try {
          state = await session.read(named, { signal });
        } catch (error) {
          if (signal.aborted || !answered(error)) {
            return;
          }
          throw stopped(reasonOf(error));
        }
        if (state.closed) {
          for (const event of state.events) {
            message(event);
          }
          throw stopped(state.closed);
        }
        let unread: readonly object[] = state.events;
        let cursor = state.cursor;
        let gap = false;
        let yieldTurn: () => void = () => undefined;
        let passed = new Set<string>();
        while (!signal.aborted && !gap) {
          const turn = new AbortController();
          const mode = delivery();
          const replaced = Promise.withResolvers<false>();
          yieldTurn = () => replaced.resolve(false);
          const skip = passed;
          let recording: Set<string> | undefined;
          const switched = () => {
            if (delivery() !== mode) {
              replaced.resolve(false);
            }
          };
          view.addEventListener("focus", switched);
          view.addEventListener("blur", switched);
          try {
            const events = await session.follow(named, {
              signal: AbortSignal.any([signal, turn.signal]),
              cursor,
              delivery: mode,
              onGap: () => {
                gap = true;
                yieldTurn();
              },
            });
            open();
            held?.turn.abort();
            await held?.drained;
            held = undefined;
            for (const event of unread) {
              message(event);
            }
            unread = [];
            const drained = (async () => {
              for await (const { eventId, data } of events) {
                if (gap || skip.delete(eventId)) {
                  continue;
                }
                recording?.add(eventId);
                message(data);
              }
              return true;
            })();
            const ended = await Promise.race([drained, replaced.promise]);
            cursor = events.cursor ?? cursor;
            if (!ended) {
              recording = passed = new Set();
              held = { turn, drained: drained.catch(() => undefined) };
            }
          } catch (error) {
            if (signal.aborted || !answered(error)) {
              return;
            }
            throw stopped(reasonOf(error));
          } finally {
            view.removeEventListener("focus", switched);
            view.removeEventListener("blur", switched);
          }
        }
      }
    } finally {
      held?.turn.abort();
    }
  };
}
