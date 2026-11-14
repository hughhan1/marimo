/* Copyright 2026 Marimo. All rights reserved. */

/** One event of a host stream, before validation. */
export interface Frame {
  readonly event: string;
  readonly data: unknown;
  readonly id?: string;
}

/**
 * A stream stopped and will not reconnect.
 *
 * `cause` says why when there is more to say.
 *
 * - The host refused the stream. `cause` is the `Problem` it answered with.
 * - The host sent a message that is not an event, or an invalid event.
 *   `cause` is the validation error.
 * - The host could not be reached after repeated attempts. `cause` is
 *   undefined.
 * - A custom transport decided to stop. `cause` is whatever it passed.
 */
export class StreamStopped extends Error {
  public override readonly name = "StreamStopped";
}

const CONNECTION_CLOSED = -32000;
const REQUEST_TIMEOUT = -32001;

/**
 * Whether a failure is the server's answer, which trying again would repeat:
 * an error it sent, a refusal with an HTTP 4xx status, or a tool's error
 * result. A closed connection or a timeout is not an answer.
 */
export function answered(error: unknown): boolean {
  const failure = error as {
    code?: unknown;
    status?: unknown;
    result?: unknown;
    data?: { problem?: unknown };
  } | null;
  return (
    failure?.result !== undefined ||
    failure?.data?.problem !== undefined ||
    (typeof failure?.code === "number" &&
      failure.code !== CONNECTION_CLOSED &&
      failure.code !== REQUEST_TIMEOUT) ||
    (typeof failure?.status === "number" &&
      failure.status >= 400 &&
      failure.status < 500)
  );
}

/** Where one connection of a host stream starts. */
export interface Attempt {
  /** Where to resume, or null to start with a snapshot. */
  readonly cursor: string | null;
  /** Aborted when the stream closes. The transport closes its connection. */
  readonly signal: AbortSignal;
}

/** What a transport reports while its connection is open. */
export interface Handlers {
  /** The connection is established. */
  open(): void;
  /** One decoded message, not yet validated. */
  message(message: unknown): void;
}

/**
 * Open one connection to a host stream and report it until it ends.
 *
 * The event stream calls it again for each attempt. It resolves when the
 * connection ended and another attempt makes sense. It rejects when the
 * stream should stop, preferably with a `StreamStopped` that says why, and
 * `closed` rejects with the same error. Calls to the handlers after it
 * settles are ignored. It sends the cursor however its host expects, and
 * leaves reconnecting, validation, and the cursor's value to the event stream.
 */
export type Transport = (attempt: Attempt, handlers: Handlers) => Promise<void>;

/** A custom transport for a notebook stream. */
export type NotebookTransport = Transport;
