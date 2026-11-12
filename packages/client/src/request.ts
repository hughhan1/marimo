/* Copyright 2026 Marimo. All rights reserved. */

import { createApps, type RuntimeClient } from "holon-sh/runtime";
import * as z from "zod";
import { parseProblem, type Problem, type UnknownProblem } from "./problem.ts";
import { type ToolName, tools } from "./tools.ts";

/** How a client reaches and authenticates with a host. */
export interface ClientOptions {
  /** Sent as `Authorization: Bearer <token>`. */
  readonly token?: string;
  /**
   * Sends each request. Defaults to `globalThis.fetch`.
   *
   * Wrap it to add headers or change requests. It receives each request with
   * the client's own headers already set.
   */
  readonly fetch?: typeof globalThis.fetch;
}

/**
 * The outcome of a tool call the host answered.
 *
 * A call throws when the host cannot be reached or when its answer is not one
 * the protocol describes.
 */
export type Result<T, P = Problem> =
  | { readonly ok: true; readonly data: T }
  | { readonly ok: false; readonly problem: P };

/**
 * The problems a tool declares.
 *
 * A host on a later version can answer with a type this client does not
 * define, which arrives as an `UnknownProblem`.
 */
export type ProblemOf<Name extends ToolName> =
  | z.output<(typeof tools)[Name]["problems"][number]>
  | UnknownProblem;

/** A tool's result, or `undefined` when it has none. */
type Data<Name extends ToolName> =
  (typeof tools)[Name]["result"] extends z.ZodType
    ? z.output<(typeof tools)[Name]["result"]>
    : undefined;

/** What a call to one tool resolves to. */
export type ResultOf<Name extends ToolName> = Result<
  Data<Name>,
  ProblemOf<Name>
>;

/** A tool's arguments as a caller gives them: the idempotency key is optional. */
export type ArgumentsOf<Name extends ToolName> = Omit<
  z.input<(typeof tools)[Name]["arguments"]>,
  "idempotency_key"
>;

/** Options for a call. */
export interface MutationOptions {
  readonly signal?: AbortSignal;
  /**
   * The key the host uses to answer a repeated call once.
   *
   * Defaults to a new random key. Pass the same key to retry a call.
   */
  readonly idempotencyKey?: string;
}

/**
 * A call as a transport sends it: the tool's name and its arguments, flat.
 *
 * `ToolCall` is any tool's call, and `ToolCall<"notebook.edit">` is one.
 */
export type ToolCall<Name extends ToolName = ToolName> = {
  [N in Name]: { readonly operation: N } & z.output<
    (typeof tools)[N]["arguments"]
  >;
}[Name];

/**
 * Sends a call to a host and resolves with its reply, `{ ok: true, data }` or
 * `{ ok: false, problem }`.
 */
export type CallTool = (
  call: ToolCall,
  signal?: AbortSignal,
) => Promise<unknown>;

/**
 * A random key for a call that changes state.
 *
 * Send it again with a retry whose outcome is unknown, so the host applies the
 * call once. `crypto.randomUUID` exists only in secure contexts, such as
 * pages served over HTTPS or from localhost. A host on a local network is
 * often neither.
 */
export function idempotencyKey(): string {
  if (typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4),
    hex.slice(4, 6),
    hex.slice(6, 8),
    hex.slice(8, 10),
    hex.slice(10),
  ]
    .map((group) => group.join(""))
    .join("-");
}

/** Whether a `Content-Type` names `type`, ignoring case and parameters. */
export function isType(contentType: string | null, type: string): boolean {
  return contentType?.split(";", 1)[0].trim().toLowerCase() === type;
}

/**
 * A URL below `url`, keeping its query string.
 *
 * `suffix` is empty or starts with a slash, as the protocol's paths do.
 */
export function below(url: URL, suffix: string): URL {
  const target = new URL(url);
  target.pathname = url.pathname.replace(/\/$/, "") + suffix;
  return target;
}

const identity = { name: "@marimo-team/client", version: "0.1.0" };

/**
 * A host's tools at an MCP `endpoint`, such as `/mcp` below the host API.
 *
 * A failure carries the problem as its text.
 */
export function mcp(
  endpoint: URL,
  { token, fetch }: ClientOptions = {},
): CallTool {
  let client: RuntimeClient | undefined;
  return async ({ operation, ...args }, signal) => {
    client ??= createApps(
      identity,
      { host: null },
      {
        host: {
          endpoint,
          ...(token === undefined
            ? {}
            : { authProvider: { token: async () => token } }),
          fetch,
        },
      },
    ).host;
    const result = await client.callTool(
      { name: operation, arguments: args },
      { signal },
    );
    if (result.resultType !== "complete") {
      throw new Error(`${operation} asked a question no host tool asks`);
    }
    if (!result.isError) {
      return { ok: true, data: result.structuredContent ?? {} };
    }
    const [block] = result.content;
    let problem: unknown;
    try {
      problem = block?.type === "text" ? JSON.parse(block.text) : undefined;
    } catch {
      problem = undefined;
    }
    if (typeof problem !== "object" || problem === null) {
      throw new Error(`${operation} failed without a problem`);
    }
    return { ok: false, problem };
  };
}

/** What a transport resolves a call with: the tool's reply, or its problem. */
const Reply = z.union([
  z.object({ ok: z.literal(true), data: z.unknown() }),
  z.object({ ok: z.literal(false), problem: z.unknown() }),
]);

/** Wait for `pending`, and stop waiting on abort. A call already sent may still apply. */
function abortable<T>(pending: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal === undefined) {
    return pending;
  }
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    pending
      .then(resolve, reject)
      .finally(() => signal.removeEventListener("abort", abort));
  });
}

/**
 * Call one of a host's tools.
 *
 * The arguments are validated before they are sent, and the reply against the
 * tool's result or the problems it declares, so the data has the type the
 * tool describes.
 */
export async function call<Name extends ToolName>(
  send: CallTool,
  name: Name,
  args: ArgumentsOf<Name>,
  { signal, idempotencyKey: key = idempotencyKey() }: MutationOptions = {},
): Promise<ResultOf<Name>> {
  const tool = tools[name];
  const parsed: object = tool.arguments.parse({
    idempotency_key: key,
    ...args,
  });
  signal?.throwIfAborted();
  // SAFETY: `parsed` is the output of `name`'s arguments schema.
  const message = { operation: name, ...parsed } as unknown as ToolCall;
  const reply = Reply.parse(await abortable(send(message, signal), signal));
  if (reply.ok) {
    // SAFETY: `Data<Name>` is the output of the tool's result schema, or
    // `undefined` when it has none.
    const data = (
      tool.result === null ? undefined : tool.result.parse(reply.data)
    ) as Data<Name>;
    return { ok: true, data };
  }
  const problem = parseProblem(reply.problem);
  if (problem.type === "unknown") {
    return { ok: false, problem };
  }
  for (const schema of tool.problems) {
    const declared = schema.safeParse(problem);
    if (declared.success) {
      // SAFETY: `schema` is one the table lists for `name`.
      return { ok: false, problem: declared.data as ProblemOf<Name> };
    }
  }
  throw new Error(`Unexpected problem ${problem.type} from ${name}`);
}
