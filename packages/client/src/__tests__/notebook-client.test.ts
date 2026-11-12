/* Copyright 2026 Marimo. All rights reserved. */

import NodeFs from "node:fs";
import NodeUrl from "node:url";
import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";
import { z } from "zod";
import { NotebookClient } from "../notebook/client.ts";
import type { ProblemOf, ToolCall } from "../request.ts";
import type { ToolName } from "../tools.ts";
import * as Protocol from "../protocol.ts";

const fixtures = new NodeUrl.URL(
  "../../../../protocols/host/fixtures/",
  import.meta.url,
);

function readFixture(name: string): unknown {
  return JSON.parse(
    NodeFs.readFileSync(new NodeUrl.URL(name, fixtures), "utf8"),
  );
}

/** A notebook client whose calls `reply` answers. */
function over(reply: (call: ToolCall) => unknown) {
  const send = vi.fn(async (call: ToolCall) => reply(call));
  const notebook = new NotebookClient("n1", send);
  return { notebook, send };
}

const failing = (problem: unknown) => over(() => ({ ok: false, problem }));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("NotebookClient", () => {
  it("sends each method as its tool's call", async () => {
    const response = readFixture("edit-response.json");
    const { notebook, send } = over(() => ({ ok: true, data: response }));
    const { changes } = readFixture("edit.json") as Protocol.EditRequest;

    expect(await notebook.edit(changes)).toEqual({ ok: true, data: response });
    expect(send).toHaveBeenLastCalledWith(
      {
        operation: "notebook.edit",
        notebook_id: "n1",
        idempotency_key: expect.stringMatching(/^[\da-f-]{36}$/),
        changes,
      },
      undefined,
    );

    await notebook.save({
      expectedEtag: "etag-1",
      decisions: { environment: "keep" },
      idempotencyKey: "retry-1",
    });
    expect(send).toHaveBeenLastCalledWith(
      {
        operation: "notebook.save",
        notebook_id: "n1",
        idempotency_key: "retry-1",
        expected_etag: "etag-1",
        decisions: { environment: "keep" },
      },
      undefined,
    );
  });

  it("names the input, run and value it acts on", async () => {
    const { notebook, send } = over(() => ({
      ok: true,
      data: readFixture("input-reply-response.json"),
    }));
    await notebook.input("i/1", { value: "" }, { idempotencyKey: "reply-1" });
    expect(send).toHaveBeenLastCalledWith(
      {
        operation: "runtime.input",
        notebook_id: "n1",
        input_id: "i/1",
        value: "",
        idempotency_key: "reply-1",
      },
      undefined,
    );
    await notebook.cancel({ runId: "r1", idempotencyKey: "k" }).catch(() => {});
    expect(send).toHaveBeenLastCalledWith(
      {
        operation: "runtime.cancel",
        notebook_id: "n1",
        run_id: "r1",
        idempotency_key: "k",
      },
      undefined,
    );
  });

  it.each(["run", "execute"] as const)(
    "%s submits its arguments with call options",
    async (method) => {
      const cells = [{ id: "c1", etag: "etag-1" }];
      const code = "print('hi')";
      const run: Protocol.Run = {
        id: "run-1",
        notebook_id: "n1",
        runtime_id: "rt-1",
        cells: method === "run" ? ["c1"] : [],
        status: "queued",
        started_at: null,
        completed_at: null,
        outcome: null,
      };
      const { notebook, send } = over(() => ({ ok: true, data: run }));
      const controller = new AbortController();
      const options = { signal: controller.signal, idempotencyKey: "run-key" };
      const result =
        method === "run"
          ? await notebook.run({ cells, ...options })
          : await notebook.execute({ code, ...options });

      expect(result).toEqual({ ok: true, data: run });
      expect(send).toHaveBeenLastCalledWith(
        {
          operation: method === "run" ? "runtime.run" : "runtime.execute",
          notebook_id: "n1",
          idempotency_key: "run-key",
          ...(method === "run" ? { cells } : { code }),
        },
        controller.signal,
      );
      controller.abort();
      const aborted = { signal: controller.signal };
      await expect(
        method === "run"
          ? notebook.run({ cells, ...aborted })
          : notebook.execute({ code, ...aborted }),
      ).rejects.toBe(controller.signal.reason);
    },
  );

  it("accepts a tool without a result", async () => {
    const { notebook } = over(() => ({ ok: true, data: {} }));
    expect(await notebook.delete()).toEqual({ ok: true, data: undefined });
  });

  it("narrows a failed edit by its problem type", async () => {
    const problem = readFixture("problems/precondition-failed.json");
    const result = await failing(problem).notebook.edit([]);
    if (
      result.ok ||
      result.problem.type !== "urn:marimo:problem:precondition-failed"
    ) {
      throw new Error("Expected a failed precondition");
    }
    expectTypeOf(
      result.problem.document,
    ).toEqualTypeOf<Protocol.DocumentOutput>();
    expectTypeOf(
      result.problem.cell,
    ).toEqualTypeOf<Protocol.CellOutput | null>();
    expectTypeOf(result.problem.index).toEqualTypeOf<number>();
    expect(result.problem).toEqual(problem);
    expectTypeOf<
      Extract<
        ProblemOf<"notebook.delete">,
        { type: "urn:marimo:problem:precondition-failed" }
      >
    >().toEqualTypeOf<never>();
  });

  it.each(["conflict", "precondition-failed"])(
    "returns a %s problem as a value",
    async (name) => {
      const problem = readFixture(`problems/${name}.json`);
      expect(await failing(problem).notebook.edit([])).toEqual({
        ok: false,
        problem,
      });
    },
  );

  it.each(["index", "cell", "document"])(
    "rejects a known problem missing %s",
    async (field) => {
      const problem = Protocol.PreconditionFailedProblem.parse(
        readFixture("problems/precondition-failed.json"),
      );
      const incomplete = Object.fromEntries(
        Object.entries(problem).filter(([key]) => key !== field),
      );
      await expect(
        failing(incomplete).notebook.edit([]),
      ).rejects.toBeInstanceOf(z.ZodError);
    },
  );

  it("keeps future problem types separate from known variants", async () => {
    const problem = {
      type: "urn:example:quota",
      status: 429,
      title: "Quota exceeded",
      detail: "Retry later",
    };
    expect(await failing(problem).notebook.edit([])).toEqual({
      ok: false,
      problem: {
        type: "unknown",
        uri: problem.type,
        status: 429,
        title: problem.title,
        detail: problem.detail,
      },
    });
  });

  it("rejects a defined problem that the tool does not declare", async () => {
    const problem = readFixture("problems/precondition-failed.json");
    await expect(failing(problem).notebook.delete()).rejects.toThrow(
      "Unexpected problem",
    );
  });

  it("validates arguments before sending them", async () => {
    const { notebook, send } = over(() => ({ ok: true, data: {} }));
    await expect(notebook.run({ cells: [] })).rejects.toBeInstanceOf(
      z.ZodError,
    );
    expect(send).not.toHaveBeenCalled();
  });

  it("throws on replies the protocol does not describe", async () => {
    await expect(over(() => ({})).notebook.open()).rejects.toBeInstanceOf(
      z.ZodError,
    );
    await expect(
      over(() => ({ ok: true, data: { unexpected: true } })).notebook.open(),
    ).rejects.toBeInstanceOf(z.ZodError);
  });

  it("stops waiting on abort without sending again", async () => {
    let answer!: (reply: unknown) => void;
    const { notebook, send } = over(
      () => new Promise((resolve) => (answer = resolve)),
    );
    const controller = new AbortController();
    const editing = notebook.edit([], { signal: controller.signal });
    controller.abort();
    await expect(editing).rejects.toBe(controller.signal.reason);
    answer({ ok: true, data: readFixture("edit-response.json") });
    await expect(notebook.edit([], { signal: controller.signal })).rejects.toBe(
      controller.signal.reason,
    );
    expect(send).toHaveBeenCalledOnce();
  });

  it("makes idempotency keys outside secure contexts", async () => {
    const { getRandomValues } = globalThis.crypto;
    vi.stubGlobal("crypto", {
      getRandomValues: getRandomValues.bind(globalThis.crypto),
    });
    const { notebook, send } = over(() => ({ ok: true, data: {} }));
    await notebook.stop().catch(() => undefined);
    expect(send.mock.calls[0][0].idempotency_key).toMatch(
      /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/,
    );
  });

  it("follows no stream unless a host client made it", () => {
    expect(() => over(() => ({})).notebook.events()).toThrow("no stream");
  });

  it("has a method for each notebook tool", () => {
    // A new tool on a notebook fails this until it has a method.
    expectTypeOf({
      "notebook.update": "update",
      "notebook.delete": "delete",
      "notebook.open": "open",
      "notebook.edit": "edit",
      "notebook.save": "save",
      "runtime.start": "start",
      "runtime.stop": "stop",
      "runtime.restart": "restart",
      "runtime.run": "run",
      "runtime.execute": "execute",
      "runtime.input": "input",
      "runtime.interrupt": "interrupt",
      "runtime.cancel": "cancel",
    } as const).toExtend<
      Record<Exclude<ToolName, "notebook.create">, keyof NotebookClient>
    >();
  });
});
