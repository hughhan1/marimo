/* Copyright 2026 Marimo. All rights reserved. */

import type { RuntimeClient } from "holon-sh/runtime";
import { parseProblem } from "../problem.ts";
import { type ClientOptions, host } from "../request.ts";
import type { EventName } from "../tools.ts";
import { StreamStopped, type Transport } from "./frame.ts";

/**
 * Why a subscription stopped for good, if the host answered at all: the
 * problem it was refused or ended with, or the error it answered. A lost
 * connection is not an answer, and the stream subscribes again.
 */
function stopped(error: unknown): StreamStopped | undefined {
  const failure = error as {
    code?: unknown;
    message?: unknown;
    status?: unknown;
    data?: { problem?: unknown };
  } | null;
  if (failure?.data?.problem !== undefined) {
    const problem = parseProblem(failure.data.problem);
    return new StreamStopped(problem.detail ?? problem.title, {
      cause: problem,
    });
  }
  // A JSON-RPC error, such as arguments the host refuses, or an HTTP refusal
  // before the request reached the event, such as for a wrong token. The MCP
  // SDK reports a closed connection and a timeout as -32000 and -32001.
  const refused =
    (typeof failure?.code === "number" &&
      ![-32000, -32001].includes(failure.code)) ||
    (typeof failure?.status === "number" &&
      failure.status >= 400 &&
      failure.status < 500);
  if (refused) {
    return new StreamStopped(
      typeof failure?.message === "string"
        ? failure.message
        : "The host refused the subscription",
      { cause: error },
    );
  }
  return undefined;
}

/**
 * Follow one of a host's events at its MCP `endpoint`.
 *
 * Each attempt subscribes with `args` and the attempt's cursor, and reports
 * each event the subscription carries. The subscription reconnects by itself
 * with the same arguments. One the host refuses, or ends with a problem,
 * stops the stream; one that cannot be made ends the attempt, and the stream
 * subscribes again from its cursor.
 */
export function subscribe(
  endpoint: URL,
  options: ClientOptions,
  name: EventName,
  args: Readonly<Record<string, unknown>>,
): Transport {
  let client: RuntimeClient | undefined;
  return async ({ cursor, signal }, { open, message }) => {
    client ??= host(endpoint, options);
    try {
      const events = await client.ext.events.subscribe(
        name,
        { ...args, ...(cursor === null ? {} : { cursor }) },
        { signal, delivery: "push" },
      );
      open();
      for await (const { data } of events) {
        message(data);
      }
    } catch (error) {
      if (signal.aborted) {
        return;
      }
      const stop = stopped(error);
      if (stop !== undefined) {
        throw stop;
      }
    }
  };
}
