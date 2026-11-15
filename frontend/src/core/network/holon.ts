/* Copyright 2026 Marimo. All rights reserved. */

import { z } from "zod";
import { HTTPError } from "@/utils/errors";
import { once } from "@/utils/once";
import { getRuntimeManager } from "../runtime/config";

const getMarimo = once(async () => {
  const { createApps } = await import("#holon-ui");
  const runtimeManager = getRuntimeManager();
  return createApps({
    marimo: {
      endpoint: runtimeManager.getMcpURL(),
      fetch: withHeaders(() => runtimeManager.headers()),
    },
  }).marimo;
});

type Tools = Awaited<ReturnType<typeof getMarimo>>["tools"];

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

const Failure = z.looseObject({ status: z.number() });

/** Calls one of the editor's tools, failing as the route it replaced did. */
export async function callMarimo<T>(
  call: (tools: Tools) => Promise<T>,
): Promise<T> {
  const { ToolCallError } = await import("#holon-ui");
  try {
    return await call((await getMarimo()).tools);
  } catch (error) {
    if (!(error instanceof ToolCallError)) {
      throw error;
    }
    const failure = Failure.safeParse(error.data);
    if (!failure.success) {
      throw new HTTPError(500, "Tool call failed", { detail: error.text });
    }
    const { status, ...body } = failure.data;
    throw new HTTPError(status, error.text ?? "Tool call failed", body);
  }
}
