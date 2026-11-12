/* Copyright 2026 Marimo. All rights reserved. */

import { type NotebookKind, notebookKinds } from "../kinds.ts";
import type * as Protocol from "../protocol.ts";
import { Events, type EventsOptions } from "../transport/events.ts";
import type { NotebookTransport } from "../transport/frame.ts";
import type { StreamEvent } from "../transport/stream.ts";

/** An event delivered for the requested notebook kinds, plus ready and reset. */
export type NotebookEvent<K extends string = NotebookKind> = StreamEvent<K>;

/** What a notebook stream follows. */
export interface NotebookEventsOptions<
  K extends string = NotebookKind,
> extends EventsOptions<K> {
  /** Follow one run. A custom transport filters its own requests. */
  readonly runId?: Protocol.RunId;
}

/**
 * A notebook stream, followed across reconnects.
 *
 * Starts with a snapshot ending in `ready`, then delivers changes. A dropped
 * connection resumes from its cursor. On `reset`, discard accumulated state
 * before applying the fresh snapshot.
 */
export class NotebookEvents<
  const K extends string = NotebookKind,
> extends Events<K> {
  public constructor(
    transport: NotebookTransport,
    init: NotebookEventsOptions<K> = {},
  ) {
    super(transport, init, notebookKinds);
  }
}
