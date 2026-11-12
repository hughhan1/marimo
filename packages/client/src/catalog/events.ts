/* Copyright 2026 Marimo. All rights reserved. */

import { type CatalogKind, catalogKinds } from "../kinds.ts";
import { Events, type EventsOptions } from "../transport/events.ts";
import type { Transport } from "../transport/frame.ts";
import type { StreamEvent } from "../transport/stream.ts";

/** An event delivered for the requested catalog kinds, plus ready and reset. */
export type CatalogEvent<K extends CatalogKind = CatalogKind> = StreamEvent<K>;

/** What a catalog stream follows. */
export interface CatalogEventsOptions<
  K extends CatalogKind = CatalogKind,
> extends EventsOptions<K> {}

/**
 * The host's catalog, followed across reconnects without retaining its objects.
 *
 * `ready` ends the snapshot of what the host currently knows; projects may
 * still be indexing. On `reset`, discard accumulated state before applying
 * the fresh snapshot. Each instance has its own connection and cursor.
 */
export class CatalogEvents<
  const K extends CatalogKind = CatalogKind,
> extends Events<K> {
  public constructor(transport: Transport, init: CatalogEventsOptions<K> = {}) {
    super(transport, init, catalogKinds);
  }
}
