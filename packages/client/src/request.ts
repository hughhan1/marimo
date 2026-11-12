/* Copyright 2026 Marimo. All rights reserved. */

import * as z from "zod";
import { type Apps, createApps } from "#holon-sh";
import { type OperationId, operations } from "./operations.ts";
import { parseProblem, type Problem, type UnknownProblem } from "./problem.ts";

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
 * The outcome of a request the host answered.
 *
 * A request throws when the host cannot be reached or when its answer is not
 * one the protocol describes.
 */
export type Result<T, P = Problem> =
  | { readonly ok: true; readonly status: number; readonly data: T }
  | { readonly ok: false; readonly status: number; readonly problem: P };

/**
 * The problems an operation declares.
 *
 * A host on a later version can answer with a type this client does not
 * define, which arrives as an `UnknownProblem`.
 */
export type ProblemOf<Id extends OperationId> =
  | z.output<Declared<Id>[keyof Declared<Id>][number]>
  | UnknownProblem;

type Declared<Id extends OperationId> = {
  readonly [
    Status in keyof (typeof operations)[Id]["problems"]
  ]: (typeof operations)[Id]["problems"][Status] extends readonly z.ZodType[]
    ? (typeof operations)[Id]["problems"][Status]
    : never;
};

/** What a request for one operation resolves to. */
export type ResultOf<Id extends RequestOperationId> = Result<
  Data<Id>,
  ProblemOf<Id>
>;

/** Options for a request that changes state. */
export interface MutationOptions {
  readonly signal?: AbortSignal;
  /**
   * The key the host uses to answer a repeated request once.
   *
   * Defaults to a new random key. Pass the same key to retry a request.
   */
  readonly idempotencyKey?: string;
}

/** The operations a client calls as requests rather than streams. */
type RequestOperationId = {
  [Id in OperationId]: (typeof operations)[Id]["responses"] extends {
    200: "text/event-stream";
  }
    ? never
    : Id;
}[OperationId];

/** The request operations whose paths start with `prefix`. */
export type OperationsBelow<Prefix extends string> = {
  [Id in RequestOperationId]: (typeof operations)[Id]["path"] extends
    | Prefix
    | `${Prefix}/${string}`
    ? Id
    : never;
}[RequestOperationId];

/**
 * Where a client's requests go: the host's tools at `/mcp` below the host
 * API's URL.
 *
 * `url` stands for `prefix`, so the values of `prefix`'s placeholders, such
 * as the notebook's id, are read from `url`.
 */
export interface Target<Prefix extends string> {
  readonly url: URL;
  readonly prefix: Prefix;
  readonly client: ClientOptions;
}

/**
 * Where a client's requests go as calls handed to `send` rather than to the
 * host's tools, such as a host running in the page.
 *
 * `params` holds the values a URL would carry for `prefix`, such as the
 * notebook's id, so each call names every one of its operation's ids.
 */
export interface CallTarget<Prefix extends string> {
  readonly send: (call: CallMessage) => Promise<unknown>;
  readonly prefix: Prefix;
  readonly params: Readonly<Record<string, string>>;
}

/**
 * A call as `request` builds it, before its type is narrowed to its
 * operation: the operation's id and its tool's arguments, flat.
 */
export interface CallMessage {
  readonly operation: RequestOperationId;
  readonly [argument: string]: unknown;
}

/** What a request reads from each entry of the operations table. */
interface Operation {
  readonly method: string;
  readonly path: string;
  readonly headers: Readonly<Record<string, boolean>>;
  readonly body: z.ZodType | null;
  readonly responses: Readonly<
    Record<number, z.ZodType | null | "text/event-stream">
  >;
  readonly problems: Readonly<Record<number, readonly z.ZodType[]>>;
}

const table: Readonly<Record<OperationId, Operation>> = operations;

type Entry<Id extends RequestOperationId> = (typeof operations)[Id];

/** The data of each success response an operation describes. */
type Data<Id extends RequestOperationId> = {
  [
    Status in keyof Entry<Id>["responses"]
  ]: Entry<Id>["responses"][Status] extends z.ZodType
    ? z.output<Entry<Id>["responses"][Status]>
    : undefined;
}[keyof Entry<Id>["responses"]];

/** The placeholder names in a path, such as `notebook_id`. */
type PathParams<Path extends string> =
  Path extends `${string}{${infer Name}}${infer Rest}`
    ? Name | PathParams<Rest>
    : never;

/** A header's name as a tool argument: `If-Match` is `if_match`. */
type Argument<Name extends string> =
  Lowercase<Name> extends `${infer Head}-${infer Tail}`
    ? `${Head}_${Argument<Tail>}`
    : Lowercase<Name>;

/** An operation's headers as arguments, with the required ones required. */
type CallHeaders<Headers> = {
  readonly [
    Name in keyof Headers as Headers[Name] extends true
      ? Argument<Name & string>
      : never
  ]: string;
} & {
  readonly [
    Name in keyof Headers as Headers[Name] extends true
      ? never
      : Argument<Name & string>
  ]?: string;
};

/**
 * An operation as one message: its id and its tool's arguments, which are
 * the ids it acts on, its headers and its body's fields, flattened.
 */
export type OperationCall<Id extends RequestOperationId> = {
  [K in Id]: { readonly operation: K } & {
    readonly [Name in PathParams<Entry<K>["path"]>]: string;
  } & CallHeaders<Entry<K>["headers"]> &
    (Entry<K>["body"] extends z.ZodType ? z.output<Entry<K>["body"]> : unknown);
}[Id];

/** What a request sends besides its operation. */
interface Call<Id extends RequestOperationId> {
  /** Values for the placeholders in the path below the target's prefix. */
  readonly params?: Readonly<Record<string, string>>;
  readonly body?: Entry<Id>["body"] extends z.ZodType
    ? z.input<Entry<Id>["body"]>
    : undefined;
  readonly headers?: Readonly<Record<string, string | undefined>>;
  readonly options?: MutationOptions;
}

/**
 * A random key for a request that changes state.
 *
 * Send it again with a retry whose outcome is unknown, so the host applies the
 * request once. `crypto.randomUUID` exists only in secure contexts, such as
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

/** The URL an operation's path resolves to below the target. */
export function resolve<Prefix extends string>(
  target: Target<Prefix>,
  path: string,
  params: Readonly<Record<string, string>>,
): URL {
  const suffix = path
    .slice(target.prefix.length)
    .replace(/\{(\w+)\}/g, (_, name: string) => {
      const value = params[name];
      // An empty or dot segment would name a different resource, and
      // percent-encoding does not stop URLs from resolving dot segments.
      if (
        value === undefined ||
        value === "" ||
        value === "." ||
        value === ".."
      ) {
        throw new Error(`${path} needs a valid ${name}`);
      }
      return encodeURIComponent(value);
    });
  return below(target.url, suffix);
}

/** A host's answer, read the same way whichever way the request went. */
interface Answer {
  readonly status: number;
  /** Whether the answer describes itself as a problem. */
  readonly problem: boolean;
  json(): Promise<unknown>;
}

/** What a call handed to `send` resolves with: the tool's reply, or its problem. */
const Reply = z.union([
  z.object({ ok: z.literal(true), data: z.unknown() }),
  z.object({ ok: z.literal(false), problem: z.unknown() }),
]);

/**
 * Send one request and decode the host's answer.
 *
 * The body is validated before it is sent, and the response is validated
 * against the schema the protocol gives its status, so the data has the type
 * the operation describes.
 */
export async function request<
  Prefix extends string,
  Id extends OperationsBelow<Prefix>,
>(
  target: Target<Prefix> | CallTarget<Prefix>,
  id: Id,
  { params = {}, body, headers = {}, options = {} }: Call<Id>,
): Promise<ResultOf<Id>> {
  const operation = table[id];
  const sent: Record<string, string> = {};
  if ("idempotency-key" in operation.headers) {
    sent["idempotency-key"] = options.idempotencyKey ?? idempotencyKey();
  }
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined) {
      sent[name] = value;
    }
  }
  const payload =
    operation.body === null ? undefined : operation.body.parse(body);
  const args: Record<string, unknown> = {
    ...("send" in target ? target.params : prefixParams(target)),
    ...params,
    ...Object.fromEntries(
      Object.entries(sent).map(([name, value]) => [argument(name), value]),
    ),
    // SAFETY: every request body the protocol defines is an object.
    ...(payload as object | undefined),
  };
  const answer =
    "send" in target
      ? await call(
          target,
          operation,
          { operation: id, ...args },
          options.signal,
        )
      : await toolAnswer(target, operation, id, args, options.signal);
  const status = answer.status;
  const schema = operation.responses[status];
  if (schema === null) {
    // SAFETY: a `null` schema is how the table marks a status with no
    // content, and `Data<Id>` maps that status to `undefined`.
    return { ok: true, status, data: undefined as Data<Id> };
  }
  if (schema !== undefined && schema !== "text/event-stream") {
    // SAFETY: `schema` is the table's schema for this status of `id`, and
    // `Data<Id>` is the union of those schemas' outputs.
    const data = schema.parse(await answer.json()) as Data<Id>;
    return { ok: true, status, data };
  }
  if ((status < 200 || status >= 300) && answer.problem) {
    const problem = parseProblem(await answer.json());
    if (problem.status !== status) {
      throw new Error(
        `Problem status ${problem.status} does not match HTTP status ${status}`,
      );
    }
    if (problem.type === "unknown") {
      return { ok: false, status, problem };
    }
    for (const schema of operation.problems[status] ?? []) {
      const parsed = schema.safeParse(problem);
      if (parsed.success) {
        // SAFETY: `schema` is one the table lists for this status of `id`.
        return { ok: false, status, problem: parsed.data as ProblemOf<Id> };
      }
    }
    throw new Error(`Unexpected problem ${problem.type} response to ${id}`);
  }
  throw new Error(`Unexpected ${status} response to ${id}`);
}

/** A header's name as its tool argument: `If-Match` is `if_match`. */
function argument(header: string): string {
  return header.toLowerCase().replaceAll("-", "_");
}

/** The values of the prefix's placeholders, from the URL that stands for it. */
function prefixParams<Prefix extends string>(
  target: Target<Prefix>,
): Record<string, string> {
  const template = target.prefix.split("/").slice(1);
  const segments = target.url.pathname.replace(/\/$/, "").split("/");
  const tail = segments.slice(segments.length - template.length);
  const values: Record<string, string> = {};
  template.forEach((segment, index) => {
    const name = /^\{(\w+)\}$/.exec(segment)?.[1];
    if (name !== undefined) {
      values[name] = decodeURIComponent(tail[index]);
    }
  });
  return values;
}

/** The host API's URL, which the target's URL is below by its prefix. */
function hostUrl<Prefix extends string>(target: Target<Prefix>): URL {
  const depth = target.prefix.split("/").length - 1;
  const segments = target.url.pathname.replace(/\/$/, "").split("/");
  const url = new URL(target.url);
  url.pathname = segments.slice(0, segments.length - depth).join("/");
  return url;
}

/** The success status the protocol gives an operation's reply. */
function successStatus(operation: Operation): number {
  return Number(
    Object.keys(operation.responses).find((status) => status.startsWith("2")),
  );
}

const clients = new WeakMap<ClientOptions, Map<string, Apps["host"]>>();

/**
 * The host's tools at `/mcp` below the host API's URL.
 *
 * Clients that share their options share one tool client per host.
 */
function tools<Prefix extends string>(target: Target<Prefix>): Apps["host"] {
  const endpoint = below(hostUrl(target), "/mcp");
  let byEndpoint = clients.get(target.client);
  if (byEndpoint === undefined) {
    byEndpoint = new Map();
    clients.set(target.client, byEndpoint);
  }
  let client = byEndpoint.get(endpoint.href);
  if (client === undefined) {
    const { token, fetch } = target.client;
    client = createApps({
      host: {
        endpoint,
        authProvider: { token: async () => token },
        fetch,
      },
    }).host;
    byEndpoint.set(endpoint.href, client);
  }
  return client;
}

/**
 * Call an operation's tool.
 *
 * Its reply is the data of the operation's success, and a failure carries
 * the problem the protocol describes as its text.
 */
async function toolAnswer<Prefix extends string>(
  target: Target<Prefix>,
  operation: Operation,
  id: RequestOperationId,
  args: Record<string, unknown>,
  signal: AbortSignal | undefined,
): Promise<Answer> {
  const result = await tools(target).callTool(
    // SAFETY: `args` are the operation's arguments, which the host checks.
    { name: id, arguments: args as never },
    { signal },
  );
  if (result.resultType !== "complete") {
    throw new Error(`${id} asked a question no host operation asks`);
  }
  if (result.isError) {
    const [block] = result.content;
    let problem: unknown;
    try {
      problem = block?.type === "text" ? JSON.parse(block.text) : undefined;
    } catch {
      problem = undefined;
    }
    if (typeof problem !== "object" || problem === null) {
      throw new Error(`${id} failed without a problem`);
    }
    return {
      status: z.object({ status: z.number().int() }).parse(problem).status,
      problem: true,
      json: async () => problem,
    };
  }
  return {
    status: successStatus(operation),
    problem: false,
    json: async () => result.structuredContent,
  };
}

/** Deliver a call, and stop waiting on abort. A call already sent may still apply. */
async function call<Prefix extends string>(
  target: CallTarget<Prefix>,
  operation: Operation,
  message: CallMessage,
  signal: AbortSignal | undefined,
): Promise<Answer> {
  signal?.throwIfAborted();
  const sending = target.send(message);
  const reply = Reply.parse(
    signal === undefined
      ? await sending
      : await new Promise((resolve, reject) => {
          const abort = () => reject(signal.reason);
          signal.addEventListener("abort", abort, { once: true });
          sending
            .then(resolve, reject)
            .finally(() => signal.removeEventListener("abort", abort));
        }),
  );
  if (reply.ok) {
    return {
      status: successStatus(operation),
      problem: false,
      json: async () => reply.data,
    };
  }
  const { problem } = reply;
  return {
    status: z.object({ status: z.number().int() }).parse(problem).status,
    problem: true,
    json: async () => problem,
  };
}
