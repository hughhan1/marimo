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
async function* following(...frames: object[]) {
  for (const frame of frames) {
    yield { data: { frame: JSON.stringify(frame) } };
  }
  await new Promise(() => undefined);
}

/** A subscription that fails at once. */
function failing(error: unknown): AsyncIterable<{ data: never }> {
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
  it("subscribes with the session arguments and delivers protocol events", async () => {
    const subscribe = vi.fn<SubscribeSession>(async () =>
      following({ event: "reset", data: {} }),
    );
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, subscribe),
      { types: ["reset"] },
    );
    const received = collect(events);
    await vi.waitFor(() => expect(received).toHaveLength(1));
    expect(subscribe.mock.calls[0][0]).toEqual(ARGS);
    expect(received).toEqual([{ event: "reset", data: {} }]);
    events.close();
  });

  it("releases a held slot when a subscription attempt fails", async () => {
    let held: AbortSignal | undefined;
    const slots = {
      hold: async (signal: AbortSignal) => {
        held = signal;
        return signal;
      },
    };
    const transport = sessionEvents(
      async () => ARGS,
      async () => {
        throw new Error("offline");
      },
      slots,
    );
    await transport(
      { cursor: null, signal: new AbortController().signal },
      { open: vi.fn(), message: vi.fn() },
    );
    expect(held?.aborted).toBe(true);
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

  it.each([undefined, null, "refused"])(
    "stops when the caller may not follow the session (data: %s)",
    async (data) => {
      const events = new NotebookEvents(
        sessionEvents(
          async () => ARGS,
          async () => {
            throw Object.assign(new Error("Forbidden"), { code: -32024, data });
          },
        ),
      );
      await expect(events.closed).rejects.toBeInstanceOf(StreamStopped);
    },
  );

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
