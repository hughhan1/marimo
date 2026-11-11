/* Copyright 2026 Marimo. All rights reserved. */

import { NotebookEvents, StreamStopped } from "@marimo-team/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type SessionArguments,
  type SubscribeSession,
  sessionEvents,
} from "../server";

const ARGS: SessionArguments = { session_id: "s1", query_params: "" };

/** A subscription that yields these frames, then waits. */
async function* following(...frames: string[]) {
  for (const frame of frames) {
    yield { data: { frame } };
  }
  await new Promise(() => undefined);
}

/** A subscription that fails at once. */
function failing(error: unknown): AsyncIterable<{ data: { frame: string } }> {
  return {
    [Symbol.asyncIterator]: () => ({ next: () => Promise.reject(error) }),
  };
}

function collect(events: NotebookEvents<string>) {
  const received: unknown[] = [];
  events.subscribe((event) => received.push(event));
  return received;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("sessionEvents", () => {
  it("subscribes with the session's arguments and reads frames with marimo's parser", async () => {
    const subscribe = vi.fn<SubscribeSession>(async () =>
      following('{"event":"marimo","id":"7","data":{"marimo":{"value":NaN}}}'),
    );
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, subscribe),
      { types: ["marimo"] },
    );
    const received = collect(events);
    await vi.waitFor(() => expect(received).toHaveLength(1));
    expect(subscribe.mock.calls[0][0]).toEqual(ARGS);
    expect(received).toEqual([
      { event: "marimo", id: "7", data: { marimo: { value: Number.NaN } } },
    ]);
    events.close();
  });

  it("stops when the session terminates the subscription with a final close", async () => {
    const ended = Object.assign(new Error("ended"), {
      code: -32000,
      data: { reason: "MARIMO_UNAUTHORIZED" },
    });
    const events = new NotebookEvents(
      sessionEvents(
        async () => ARGS,
        async () => failing(ended),
      ),
    );
    const error = await events.closed.catch((error: unknown) => error);
    expect(error).toBeInstanceOf(StreamStopped);
    expect((error as StreamStopped).cause).toMatchInlineSnapshot(`
      {
        "code": "KERNEL_DISCONNECTED",
        "reason": "not authorized",
        "state": "CLOSED",
      }
    `);
  });

  it("stops when the caller may not follow the session", async () => {
    const events = new NotebookEvents(
      sessionEvents(
        async () => ARGS,
        async () => {
          throw Object.assign(new Error("Forbidden"), { code: -32024 });
        },
      ),
    );
    await expect(events.closed).rejects.toBeInstanceOf(StreamStopped);
  });

  it("subscribes again after a failure that says nothing final", async () => {
    vi.useFakeTimers();
    const subscribe = vi.fn<SubscribeSession>(async () => {
      throw new Error("fetch failed");
    });
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, subscribe),
    );
    await vi.waitFor(async () => {
      await vi.advanceTimersByTimeAsync(1000);
      expect(subscribe.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    events.close();
  });
});
