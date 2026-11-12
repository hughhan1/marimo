/* Copyright 2026 Marimo. All rights reserved. */

import type * as Protocol from "../protocol.ts";
import type { NotebookKind } from "../kinds.ts";
import {
  type ArgumentsOf,
  below,
  type CallTool,
  type ClientOptions,
  call,
  type MutationOptions,
  type ResultOf,
} from "../request.ts";
import type { ToolName } from "../tools.ts";
import { NotebookEvents, type NotebookEventsOptions } from "./events.ts";

/** How a `NotebookClient` reaches and authenticates with its host. */
export type NotebookClientOptions = ClientOptions;

/** Options for a call the host may need decisions for. */
interface DecisionOptions extends MutationOptions {
  /** Answers to the decisions a `decision-required` problem asked for. */
  readonly decisions?: Readonly<Record<string, string>>;
}

/** Options for saving a notebook. */
interface SaveOptions extends DecisionOptions {
  /** The document etag the client last read. A different etag is a conflict. */
  readonly expectedEtag?: string;
}

/** Where a notebook's stream is served, when its client has one. */
interface Stream {
  readonly url: URL;
  readonly options: ClientOptions;
}

/**
 * A client for one notebook.
 *
 * Its tools go to `send`: a host's MCP endpoint, through `mcp`, or anything
 * else that answers `{ ok: true, data }` or `{ ok: false, problem }`, such as
 * a host running in the page. A client made by `Client.notebook` also follows
 * the notebook's stream.
 */
export class NotebookClient {
  readonly #id: string;
  readonly #send: CallTool;
  readonly #stream: Stream | undefined;

  public constructor(notebookId: string, send: CallTool, stream?: Stream) {
    this.#id = notebookId;
    this.#send = send;
    this.#stream = stream;
  }

  /** Rename or move the notebook. */
  public update(
    changes: Pick<Protocol.UpdateNotebookRequest, "path" | "name">,
    options?: MutationOptions,
  ): Promise<ResultOf<"notebook.update">> {
    return this.#call("notebook.update", changes, options);
  }

  /** Delete the notebook. */
  public delete(
    options?: MutationOptions,
  ): Promise<ResultOf<"notebook.delete">> {
    return this.#call("notebook.delete", {}, options);
  }

  /** Ask the host for a URL that opens the notebook for people. */
  public open(options?: MutationOptions): Promise<ResultOf<"notebook.open">> {
    return this.#call("notebook.open", {}, options);
  }

  /** Apply changes to the document as one unit. */
  public edit(
    changes: readonly Protocol.Change[],
    options?: MutationOptions,
  ): Promise<ResultOf<"notebook.edit">> {
    return this.#call("notebook.edit", { changes: [...changes] }, options);
  }

  /** Write the document to the notebook's store. */
  public save({
    decisions,
    expectedEtag,
    ...options
  }: SaveOptions = {}): Promise<ResultOf<"notebook.save">> {
    return this.#call(
      "notebook.save",
      { decisions, expected_etag: expectedEtag },
      options,
    );
  }

  /** Start a runtime for the notebook. */
  public start({ decisions, ...options }: DecisionOptions = {}): Promise<
    ResultOf<"runtime.start">
  > {
    return this.#call("runtime.start", { decisions }, options);
  }

  /** Stop the notebook's runtime. */
  public stop(options?: MutationOptions): Promise<ResultOf<"runtime.stop">> {
    return this.#call("runtime.stop", {}, options);
  }

  /** Restart the notebook's runtime. */
  public restart(
    options?: MutationOptions,
  ): Promise<ResultOf<"runtime.restart">> {
    return this.#call("runtime.restart", {}, options);
  }

  /**
   * Run notebook cells.
   *
   * The notebook's execution settings determine which dependent cells also run.
   * Follow the notebook stream for progress and results.
   */
  public run({
    cells,
    ...options
  }: Pick<Protocol.RunRequest, "cells"> & MutationOptions): Promise<
    ResultOf<"runtime.run">
  > {
    return this.#call("runtime.run", { cells }, options);
  }

  /**
   * Execute Python code.
   *
   * The code can use the notebook's current values. Assignments stay local to
   * this call, but changes to shared objects persist.
   *
   * On marimo hosts, `marimo._code_mode.get_context()` provides access to
   * cells, captured outputs, and notebook editing methods.
   * Follow the notebook stream for progress and results.
   */
  public execute({
    code,
    ...options
  }: { readonly code: string } & MutationOptions): Promise<
    ResultOf<"runtime.execute">
  > {
    return this.#call("runtime.execute", { code }, options);
  }

  /** Answer one pending input. Acceptance does not imply execution resumed. */
  public input(
    inputId: Protocol.ItemId,
    { value }: Pick<Protocol.InputReplyRequest, "value">,
    options?: MutationOptions,
  ): Promise<ResultOf<"runtime.input">> {
    return this.#call("runtime.input", { input_id: inputId, value }, options);
  }

  /** Interrupt active work and end queued runs in the notebook's runtime. */
  public interrupt(
    options?: MutationOptions,
  ): Promise<ResultOf<"runtime.interrupt">> {
    return this.#call("runtime.interrupt", {}, options);
  }

  /** Cancel one run without affecting other runs. */
  public cancel({
    runId,
    ...options
  }: { readonly runId: Protocol.RunId } & MutationOptions): Promise<
    ResultOf<"runtime.cancel">
  > {
    return this.#call("runtime.cancel", { run_id: runId }, options);
  }

  /**
   * Follow the notebook's stream.
   *
   * The stream is served below the notebook's URL over server-sent events,
   * and shares its client's options. Only a client made by `Client.notebook`
   * has one.
   */
  public events<const K extends string = NotebookKind>(
    options: Omit<NotebookEventsOptions<K>, keyof ClientOptions> = {},
  ): NotebookEvents<K> {
    if (this.#stream === undefined) {
      throw new Error("This notebook client has no stream to follow");
    }
    return new NotebookEvents<K>(below(this.#stream.url, "/events"), {
      ...this.#stream.options,
      ...options,
    });
  }

  #call<Name extends ToolName>(
    name: Name,
    args: object,
    options: MutationOptions = {},
  ): Promise<ResultOf<Name>> {
    // SAFETY: each method passes its tool's arguments besides the notebook's
    // id, which the client adds, and the key, which `call` adds.
    const all = {
      notebook_id: this.#id,
      ...args,
    } as unknown as ArgumentsOf<Name>;
    return call(this.#send, name, all, options);
  }
}
