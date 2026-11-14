/* Copyright 2026 Marimo. All rights reserved. */

import { type NotebookTransport, StreamStopped } from "@marimo-team/client";
import type { EventArguments, EventData, Outputs } from "#holon-ui";
import { closedStatus } from "./connection-state";

// The draft's Forbidden: the caller may not follow the session.
const FORBIDDEN = -32024;

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
  if (failure?.code === FORBIDDEN) {
    return "MARIMO_UNAUTHORIZED";
  }
  const reason = failure?.data?.reason;
  return typeof reason === "string" ? reason : undefined;
}

/**
 * Follow a marimo session: read its state, then follow its `session` event
 * from the state's cursor.
 *
 * The client reconnects and resumes from the cursor by itself. The page in
 * the focused window is
 * pushed each change and every other page polls, so of a browser's pages
 * only one holds a connection open. When the server skipped changes, the
 * page reads its state again. A page that switches between push and poll,
 * or reads its state again, keeps its subscription until the next one is
 * confirmed, so it stays in its session throughout.
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
    const delivered = new Set<string>();
    let held: { turn: AbortController; drained: Promise<unknown> } | undefined;
    try {
      while (!signal.aborted) {
        const state = await session.read(named, { signal });
        if (state.closed) {
          for (const event of state.events) {
            message(event);
          }
          throw stopped(state.closed);
        }
        let unread: readonly object[] = state.events;
        let cursor = state.cursor;
        let gap = false;
        while (!signal.aborted && !gap) {
          const turn = new AbortController();
          const mode = delivery();
          const replaced = Promise.withResolvers<false>();
          let current = true;
          let muted = false;
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
                if (current) {
                  gap = true;
                  muted = true;
                  replaced.resolve(false);
                }
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
                if (muted || delivered.has(eventId)) {
                  continue;
                }
                delivered.add(eventId);
                if (delivered.size > 1024) {
                  const [oldest] = delivered;
                  delivered.delete(oldest);
                }
                message(data);
              }
              return true;
            })();
            const ended = await Promise.race([drained, replaced.promise]);
            current = false;
            cursor = events.cursor ?? cursor;
            if (!ended) {
              held = { turn, drained: drained.catch(() => undefined) };
            }
          } catch (error) {
            if (!signal.aborted) {
              throw stopped(reasonOf(error));
            }
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
