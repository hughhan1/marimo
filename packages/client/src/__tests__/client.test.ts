/* Copyright 2026 Marimo. All rights reserved. */

import { describe, expect, expectTypeOf, it, vi } from "vitest";
import { z } from "zod";
import { Client, NotebookClient } from "../index.ts";
import type * as Protocol from "../protocol.ts";
import type { ProblemOf } from "../request.ts";
import { fixtureEvents, host, type Received } from "./fixtures.ts";

const record = fixtureEvents("events.sse").find(
  (event) => event.event === "notebook",
)!.data.notebook;

function connect(
  answer: (call: Received) => unknown,
  stream?: (request: Request) => Response,
) {
  const { fetch, calls, requests } = host(answer, stream);
  const client = new Client(
    "http://localhost/prefix/api/marimo/v1/?session_id=s1",
    { token: "secret", fetch },
  );
  return { client, fetch, calls, requests };
}

describe("Client", () => {
  it("creates a notebook through the host's tools", async () => {
    const { client, fetch, calls } = connect(() => record);
    const input = { project_id: "p1", path: "forecasts.py" };
    const result = await client.createNotebook(input, {
      idempotencyKey: "create-1",
    });
    expectTypeOf(result).toEqualTypeOf<
      | { readonly ok: true; readonly data: Protocol.NotebookOutput }
      | {
          readonly ok: false;
          readonly problem: ProblemOf<"notebook.create">;
        }
    >();
    expect(result).toEqual({ ok: true, data: record });
    expect(String(fetch.mock.calls[0][0])).toBe(
      "http://localhost/prefix/api/marimo/v1/mcp?session_id=s1",
    );
    expect(calls).toEqual([
      {
        name: "notebook.create",
        arguments: { ...input, idempotency_key: "create-1" },
        authorization: "Bearer secret",
      },
    ]);
  });

  it("constructs notebook clients without fetching or retaining their records", async () => {
    const { client, fetch, calls } = connect(() => ({}));
    const entry = { ...record, id: "n/1?#%" };
    const notebook = client.notebook(entry);
    expect(notebook).toBeInstanceOf(NotebookClient);
    expect(fetch).not.toHaveBeenCalled();
    entry.id = "changed";
    client.url.host = "elsewhere.example";
    await notebook.delete();
    expect(calls[0]).toMatchObject({
      name: "notebook.delete",
      arguments: { notebook_id: "n/1?#%" },
      authorization: "Bearer secret",
    });
  });

  it.each(["", ".", ".."])("rejects an invalid notebook id %j", (id) => {
    const { client, fetch } = connect(() => ({}));
    expect(() => client.notebook(id)).toThrow("valid notebook_id");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("validates arguments and returns host problems", async () => {
    const problem = {
      type: "urn:marimo:problem:conflict",
      title: "Conflict",
      status: 409,
    };
    const { client, fetch } = connect(() => ({ problem }));
    await expect(
      client.createNotebook({ project_id: "" }),
    ).rejects.toBeInstanceOf(z.ZodError);
    expect(fetch).not.toHaveBeenCalled();
    expect(
      await client.createNotebook({ project_id: "p1", path: "forecasts.py" }),
    ).toEqual({ ok: false, problem });
  });

  it("throws on a failure that carries no problem", async () => {
    const { client } = connect(() => ({ problem: "<html>" }));
    await expect(client.notebook("n1").open()).rejects.toThrow(
      "notebook.open failed without a problem",
    );
  });

  it("follows a notebook's stream below its URL", async () => {
    const { client, requests } = connect(
      () => ({}),
      () =>
        new Response("id: 1\nevent: ready\ndata: {}\n\n", {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );
    const events = client.notebook("n/1").events();
    await vi.waitFor(() => expect(events.cursor).toBe("1"));
    expect(requests[0].url).toBe(
      "http://localhost/prefix/api/marimo/v1/notebooks/n%2F1/events?session_id=s1",
    );
    events.close();
    await events.closed;
  });

  it("opens independent catalog streams with inherited connection options", async () => {
    const { client, requests, fetch } = connect(
      () => ({}),
      () =>
        new Response("id: 41\nevent: ready\ndata: {}\n\n", {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );
    const first = client.events({ types: ["notebook"], cursor: "40" });
    const second = client.events();
    await vi.waitFor(() => expect(second.cursor).toBe("41"));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(requests[0].url).toBe(
      "http://localhost/prefix/api/marimo/v1/events?session_id=s1&types=notebook",
    );
    expect(requests[0].headers.get("Authorization")).toBe("Bearer secret");
    expect(requests[0].headers.get("Last-Event-ID")).toBe("40");
    expect(requests[1].headers.get("Last-Event-ID")).toBeNull();
    first.close();
    expect(second.status).not.toBe("closed");
    second.close();
    await Promise.all([first.closed, second.closed]);
  });
});
