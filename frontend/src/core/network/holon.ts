/* Copyright 2026 Marimo. All rights reserved. */

import { UnknownProblem } from "@marimo-team/client/protocol";
import { assertExists } from "@/utils/assertExists";
import { HTTPError } from "@/utils/errors";
import { once } from "@/utils/once";
import { getRuntimeManager } from "../runtime/config";
import { activeNotebookIdAtom } from "../session/active-notebook";
import { store } from "../state/jotai";

export const getMarimo = once(async () => {
  const { connect } = await import("#holon-ui");
  const runtimeManager = getRuntimeManager();
  const page = await connect({
    web: {
      endpoint: runtimeManager.getMcpURL(),
      fileOrigins: [runtimeManager.httpURL.origin],
      fetch: withHeaders(() => runtimeManager.headers()),
    },
  });
  if (page.view !== undefined) {
    throw new Error("The editor is built as a web page, not an MCP Apps view");
  }
  return page;
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

/** The page's notebook, which a request that acts on it names. */
export function activeNotebookId(): string {
  const notebookId = store.get(activeNotebookIdAtom);
  assertExists(notebookId, "No notebook is connected");
  return notebookId;
}

/** A notebook tool's arguments, naming the page's notebook. */
export function inNotebook<A extends object>(
  args: A,
): A & { notebook_id: string } {
  return { ...args, notebook_id: activeNotebookId() };
}

/** The arguments of a tool that acts on the page's notebook if it has one. */
export function maybeInNotebook<A extends object>(
  args: A,
): A & { notebook_id?: string } {
  const notebook_id = store.get(activeNotebookIdAtom);
  return notebook_id === null ? args : { ...args, notebook_id };
}
