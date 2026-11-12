/* Copyright 2026 Marimo. All rights reserved. */

import { expectTypeOf } from "vitest";
import type * as Host from "../Host.ts";
import type * as Notebook from "../Notebook.ts";
import * as S from "../Schemas.ts";
import * as Analysis from "../experimental/Analysis.ts";

export function checkEventNarrowing(
  event: Notebook.Event,
  catalog: Host.Event,
  extended: Analysis.NotebookEvent,
) {
  if (event.event === "cell") {
    expectTypeOf(event.data.cell.id).toEqualTypeOf<typeof S.CellId.Type>();
    // @ts-expect-error A cell message contains no runtime record.
    void event.data.runtime;
  }
  // @ts-expect-error Host records belong to the catalog stream.
  if (event.event === "host") {
    return;
  }
  // @ts-expect-error Cell records belong to the notebook stream.
  if (catalog.event === "cell") {
    return;
  }
  // @ts-expect-error Analysis requires explicitly selecting the experimental contract.
  if (event.event === "analysis") {
    return;
  }
  if (extended.event === "analysis") {
    expectTypeOf(extended.data.analysis).toEqualTypeOf<
      typeof Analysis.Analysis.Type
    >();
  }
}

// @ts-expect-error Event variants are private; consume the scoped unions.
void S.ReadyEvent;
// @ts-expect-error Event envelopes are private.
void S.HostMessage;
// @ts-expect-error Analysis exposes its composed union, not an individual variant.
void Analysis.AnalysisEvent;
