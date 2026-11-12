/* Copyright 2026 Marimo. All rights reserved. */

import { CatalogEvents, type CatalogEventsOptions } from "./catalog/events.ts";
import type { CatalogKind } from "./kinds.ts";
import { NotebookClient } from "./notebook/client.ts";
import type * as Protocol from "./protocol.ts";
import {
  type ArgumentsOf,
  below,
  type CallTool,
  type ClientOptions,
  call,
  mcp,
  type MutationOptions,
  type ResultOf,
} from "./request.ts";
import { subscribe } from "./transport/mcp.ts";

/** A client for a host's API: its tools and events at `/mcp`. */
export class Client {
  readonly #url: URL;
  readonly #endpoint: URL;
  readonly #options: ClientOptions;
  readonly #send: CallTool;

  public constructor(url: string | URL, init: ClientOptions = {}) {
    this.#url = new URL(url);
    this.#endpoint = below(this.#url, "/mcp");
    this.#options = init;
    this.#send = mcp(this.#endpoint, init);
  }

  /** The host API's base URL. Each read returns a copy. */
  public get url(): URL {
    return new URL(this.#url);
  }

  /** Follow the catalog with this client's options and an independent cursor. */
  public events<const K extends CatalogKind = CatalogKind>(
    options: CatalogEventsOptions<K> = {},
  ): CatalogEvents<K> {
    const args = options.types === undefined ? {} : { types: options.types };
    return new CatalogEvents<K>(
      subscribe(this.#endpoint, this.#options, "catalog", args),
      options,
    );
  }

  /**
   * Address a notebook by id or catalog record, sharing this client's options.
   *
   * Makes no request and retains no metadata. The id belongs to this host.
   */
  public notebook(
    notebook: Protocol.NotebookId | Pick<Protocol.NotebookOutput, "id">,
  ): NotebookClient {
    const id = typeof notebook === "string" ? notebook : notebook.id;
    return new NotebookClient(id, this.#send, (args) =>
      subscribe(this.#endpoint, this.#options, "notebook", args),
    );
  }

  /** Create a notebook. Its record also arrives on the catalog stream. */
  public createNotebook(
    notebook: ArgumentsOf<"notebook.create">,
    options?: MutationOptions,
  ): Promise<ResultOf<"notebook.create">> {
    return call(this.#send, "notebook.create", notebook, options);
  }
}
