/* Copyright 2026 Marimo. All rights reserved. */

import { HTTPError } from "@/utils/errors";
import { once } from "@/utils/once";
import { getRuntimeManager } from "../runtime/config";

export const getMarimo = once(async () => {
  const { createApps } = await import("#holon-ui");
  const runtimeManager = getRuntimeManager();
  const { origin } = runtimeManager.httpURL;
  return createApps({
    marimo: {
      endpoint: new URL(
        runtimeManager.formatHttpURL({ path: "/api/mcp" }).pathname,
        origin,
      ),
      fileOrigins: [origin],
      fetch: (input: RequestInfo | URL, init?: RequestInit) => {
        const headers = new Headers(init?.headers);
        for (const [key, value] of Object.entries(runtimeManager.headers())) {
          headers.set(key, value);
        }
        return fetch(input, { ...init, headers });
      },
    },
  }).marimo;
});

type Tools = Awaited<ReturnType<typeof getMarimo>>["tools"];

/** What a failed call carries: the server's problem, as its error text. */
export interface Problem {
  type: string;
  title: string;
  detail?: string;
  status: number;
}

interface Content {
  type: string;
  text?: string;
}

function contents(result: unknown): Content[] {
  const content = (result as { content?: unknown } | null)?.content;
  return Array.isArray(content) ? (content as Content[]) : [];
}

/** The problem a failed call describes, if its text is one. */
export function problemOf(result: unknown): Problem | undefined {
  const text = contents(result).find((item) => item.type === "text")?.text;
  if (!text?.startsWith("{")) {
    return undefined;
  }
  try {
    const parsed: unknown = JSON.parse(text);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as Problem).status === "number" &&
      typeof (parsed as Problem).title === "string"
    ) {
      return parsed as Problem;
    }
  } catch {
    // Not a problem after all; the text stands as the message.
  }
  return undefined;
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

type AnyTool = (input: never, options: never) => Promise<unknown>;

/** Typed tools, whose failures keep the problem callers branch on. */
export const getTools = once(async (): Promise<Tools> => {
  const { ToolCallError } = await import("#holon-ui");
  const { tools } = await getMarimo();
  return new Proxy({} as Tools, {
    get: (_, name) =>
      // A promise resolving to this object looks for `then` on it.
      name === "then"
        ? undefined
        : (input: never, options: never) =>
            (tools[name as never] as AnyTool)(input, options).catch(
              (error: unknown) => {
                if (!(error instanceof ToolCallError)) {
                  throw error;
                }
                const problem = problemOf(error.result);
                if (problem === undefined) {
                  const text = contents(error.result).find(
                    (item) => item.type === "text",
                  )?.text;
                  throw new HTTPError(500, "Tool call failed", {
                    detail: text,
                  });
                }
                throw new HTTPError(problem.status, problem.title, problem);
              },
            ),
  });
});
