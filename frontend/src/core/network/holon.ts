/* Copyright 2026 Marimo. All rights reserved. */

import { UnknownProblem } from "@marimo-team/client/protocol";
import { HTTPError } from "@/utils/errors";
import { once } from "@/utils/once";
import { getRuntimeManager } from "../runtime/config";

export const getMarimo = once(async () => {
  const { createApps } = await import("#holon-ui");
  const runtimeManager = getRuntimeManager();
  return createApps({
    marimo: {
      endpoint: runtimeManager.getMcpURL(),
      fileOrigins: [runtimeManager.httpURL.origin],
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

/** A reply that may list files to download. */
export interface Replied {
  downloads?: readonly {
    name?: string;
    mimeType: string;
    blob(): Promise<Blob>;
  }[];
}

/** The file a tool replied with, fetched, named `fallback` when it has no name. */
export async function repliedFile(reply: Replied, fallback: string) {
  const [file] = reply.downloads ?? [];
  if (file === undefined) {
    throw new Error("The tool did not reply with a file");
  }
  return {
    contents: await file.blob(),
    filename: file.name ?? fallback,
    mediaType: file.mimeType,
  };
}

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
    const problem = UnknownProblem.safeParse(error.data);
    if (problem.success) {
      throw new HTTPError(
        problem.data.status,
        problem.data.title,
        problem.data,
      );
    }
    throw new HTTPError(500, "Tool call failed", { detail: error.text });
  }
}
