/* Copyright 2026 Marimo. All rights reserved. */

import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";
import { z } from "zod";
import { Client, NotebookClient, StreamStopped } from "../index.ts";
import type * as Protocol from "../protocol.ts";
import type { ProblemOf } from "../request.ts";
import {
  fixtureEvents,
  host,
  type Received,
  type Subscription,
} from "./fixtures.ts";

const record = fixtureEvents("events.sse").find(
  (event) => event.event === "notebook",
)!.data.notebook;

function connect(
  answer: (call: Received) => unknown,
  subscribe?: (subscription: Received) => Subscription,
) {
  const { fetch, calls, subscriptions } = host(answer, subscribe);
  const client = new Client(
    "http://localhost/prefix/api/marimo/v1/?session_id=s1",
    { token: "secret", fetch },
  );
  return { client, fetch, calls, subscriptions };
}

afterEach(() => vi.useRealTimers());

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

  it("follows the catalog with its types and the client's token", async () => {
    const { client, fetch, subscriptions } = connect(
      () => ({}),
      () => ({ events: [{ event: "ready", id: "41", data: {} }] }),
    );
    const first = client.events({ types: ["notebook"], cursor: "40" });
    const second = client.events();
    await vi.waitFor(() => {
      expect(first.cursor).toBe("41");
      expect(second.cursor).toBe("41");
    });
    expect(new Set(fetch.mock.calls.map(([url]) => String(url)))).toEqual(
      new Set(["http://localhost/prefix/api/marimo/v1/mcp?session_id=s1"]),
    );
    expect(subscriptions).toHaveLength(2);
    expect(subscriptions).toEqual(
      expect.arrayContaining([
        {
          name: "catalog",
          arguments: { types: ["notebook"], cursor: "40" },
          authorization: "Bearer secret",
        },
        { name: "catalog", arguments: {}, authorization: "Bearer secret" },
      ]),
    );
    first.close();
    expect(second.status).not.toBe("closed");
    second.close();
    await Promise.all([first.closed, second.closed]);
  });

  it("follows a notebook's events with its id and run", async () => {
    const ready = { event: "ready", id: "1", data: {} };
    const { client, subscriptions } = connect(
      () => ({}),
      () => ({ events: [ready] }),
    );
    const events = client
      .notebook("n/1")
      .events({ types: ["cell"], runId: "r1" });
    const received: unknown[] = [];
    events.subscribe((event) => received.push(event));
    await vi.waitFor(() => expect(events.cursor).toBe("1"));
    expect(received).toEqual([ready]);
    expect(subscriptions).toEqual([
      {
        name: "notebook",
        arguments: { notebook_id: "n/1", types: ["cell"], run_id: "r1" },
        authorization: "Bearer secret",
      },
    ]);
    events.close();
    await events.closed;
  });

  it("stops with the problem a host refuses a subscription with", async () => {
    const problem = {
      type: "urn:marimo:problem:not-found",
      title: "Not found",
      status: 404,
    };
    const { client, subscriptions } = connect(
      () => ({}),
      () => ({ problem }),
    );
    const events = client.notebook("n1").events();
    const error = await events.closed.catch((error: unknown) => error);
    expect(error).toBeInstanceOf(StreamStopped);
    expect((error as StreamStopped).cause).toEqual(problem);
    expect(events.status).toBe("closed");
    expect(subscriptions).toHaveLength(1);
  });
});
