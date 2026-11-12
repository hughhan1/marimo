/* Copyright 2026 Marimo. All rights reserved. */

import { Schema } from "effect";

export function sseEvents<S extends Schema.Constraint>(events: S) {
  return Schema.Struct({
    event: Schema.String,
    id: Schema.optionalKey(Schema.String),
    data: Schema.fromJsonString(Schema.Json),
  }).pipe(Schema.decodeTo(events));
}
