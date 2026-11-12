/* Copyright 2026 Marimo. All rights reserved. */

import { readFileSync } from "node:fs";
import { Effect, Schema, Stream } from "effect";
import { describe, expect, it, vi } from "vitest";
import * as Host from "../Host.ts";
import * as Problems from "../Problems.ts";
import * as S from "../Schemas.ts";

const edit = Schema.decodeUnknownSync(S.EditResponse)(
  JSON.parse(
    readFileSync(
      new URL(
        "../../../../protocols/host/fixtures/edit-response.json",
        import.meta.url,
      ),
      "utf8",
    ),
  ),
);
const unavailable = () =>
  Effect.fail(new Problems.UnsupportedOperationProblem());
const save = vi.fn((args: typeof S.SaveRequest.Type) =>
  args.expected_etag === "stale"
    ? Effect.fail(new Problems.ConflictProblem({ detail: "Stale" }))
    : Effect.succeed(edit),
);
const handlers = Host.Tools.toLayer({
  "notebook.create": unavailable,
  "notebook.delete": () => Effect.void,
  "notebook.edit": unavailable,
  "notebook.open": unavailable,
  "notebook.save": save,
  "notebook.update": unavailable,
  "runtime.cancel": unavailable,
  "runtime.execute": unavailable,
  "runtime.input": unavailable,
  "runtime.interrupt": unavailable,
  "runtime.restart": unavailable,
  "runtime.run": unavailable,
  "runtime.start": unavailable,
  "runtime.stop": unavailable,
});

/** Call a tool as a host serves it: decode its arguments, then encode its result. */
function call(name: keyof typeof Host.Tools.tools, args: unknown) {
  return Effect.runPromise(
    Effect.gen(function* () {
      const tools = yield* Host.Tools;
      const results = yield* tools.handle(name, args as never, undefined, {
        onExcessProperty: "error",
      });
      return yield* Stream.runLast(results);
    }).pipe(Effect.provide(handlers), Effect.flip, Effect.flip),
  );
}

describe("native tool implementation", () => {
  it("decodes the arguments into the typed save handler", async () => {
    const result = await call("notebook.save", {
      notebook_id: "n",
      idempotency_key: "save",
      expected_etag: "etag",
    });
    expect(result).toMatchObject({ _tag: "Some" });
    expect(save.mock.lastCall?.[0]).toEqual({
      notebook_id: "n",
      idempotency_key: "save",
      expected_etag: "etag",
    });
  });

  it.each([
    { cells: [{ id: "c" }], future: true },
    { cells: [{ id: "c", future: true }] },
  ])("rejects an argument it does not recognize: %j", async (args) => {
    await expect(
      call("runtime.run", {
        notebook_id: "n",
        idempotency_key: "run",
        ...args,
      }),
    ).rejects.toThrow("Expected no excess property");
  });

  it("rejects a missing idempotency key before calling the handler", async () => {
    const calls = save.mock.calls.length;
    await expect(call("notebook.save", { notebook_id: "n" })).rejects.toThrow(
      "Missing key",
    );
    expect(save).toHaveBeenCalledTimes(calls);
  });

  it("encodes a failure as its problem's wire fields", async () => {
    const error = await call("notebook.save", {
      notebook_id: "n",
      idempotency_key: "save",
      expected_etag: "stale",
    }).catch((failure: unknown) => failure);
    expect(Schema.encodeUnknownSync(Problems.ConflictProblem)(error)).toEqual({
      type: "urn:marimo:problem:conflict",
      status: 409,
      title: "Conflict",
      detail: "Stale",
    });
  });
});
