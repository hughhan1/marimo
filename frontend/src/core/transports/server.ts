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

/** The status of a subscription, as the client reports it. */
type Status = "connected" | "reconnecting" | "gap";

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
      onStatus: (status: Status) => void;
    },
  ): Promise<AsyncIterable<{ data: SessionEvent }> & { cursor: string | null }>;
}

/** Why following the session stopped, with the status the editor shows. */
function stopped(reason: string | undefined) {
  const status = closedStatus(reason);
  return new StreamStopped(status.reason, { cause: status });
}

/** The reason in an error the server ended or refused the subscription with. */
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
 * The client reconnects and resumes from the cursor by itself, and the page
 * shows that it is connecting meanwhile. The page in the focused window is
 * pushed each change and every other page polls, so of a browser's pages
 * only one holds a connection open. When
 * the server skipped changes, the page reads its state again.
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
  return async ({ signal }, { open, lost, message }) => {
    const named = await args();
    while (!signal.aborted) {
      const state = await session.read(named, { signal });
      if (state.closed) {
        for (const event of state.events) {
          message(event);
        }
        throw stopped(state.closed);
      }
      // The state is delivered once following it is confirmed, so the page
      // acts on it only once it is in its session.
      let unread: readonly object[] = state.events;
      let cursor = state.cursor;
      let gap = false;
      while (!signal.aborted && !gap) {
        const turn = new AbortController();
        const mode = delivery();
        const switched = () => {
          if (delivery() !== mode) {
            turn.abort();
          }
        };
        view.addEventListener("focus", switched);
        view.addEventListener("blur", switched);
        let reconnecting = false;
        let following: { cursor: string | null } | undefined;
        try {
          const events = await session.follow(named, {
            signal: AbortSignal.any([signal, turn.signal]),
            cursor,
            delivery: mode,
            onStatus: (status) => {
              if (status === "gap") {
                gap = true;
                turn.abort();
              } else if (status === "reconnecting") {
                reconnecting = true;
                lost();
              } else {
                open();
                if (reconnecting) {
                  // Resumed with nothing skipped: what the page holds is
                  // whole again, as after a snapshot.
                  reconnecting = false;
                  message({
                    event: "ready",
                    id: following?.cursor ?? cursor,
                    data: {},
                  });
                }
              }
            },
          });
          following = events;
          for (const event of unread) {
            message(event);
          }
          unread = [];
          try {
            for await (const { data } of events) {
              message(data);
            }
          } finally {
            cursor = events.cursor ?? cursor;
          }
        } catch (error) {
          if (!turn.signal.aborted && !signal.aborted) {
            throw stopped(reasonOf(error));
          }
        } finally {
          view.removeEventListener("focus", switched);
          view.removeEventListener("blur", switched);
        }
      }
    }
  };
}
