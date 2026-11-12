/* Copyright 2026 Marimo. All rights reserved. */

import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";
import { z } from "zod";
import { NotebookEvents } from "../notebook/events.ts";
import type { NotebookEvent } from "../notebook/events.ts";
import { type NotebookTransport, StreamStopped } from "../transport/frame.ts";
import { fixtureEvents, scripted } from "./fixtures.ts";

const snapshot = fixtureEvents("notebook-events.sse");
/** The fixture's events up to and including `ready`. */
const throughReady = snapshot.slice(
  0,
  snapshot.findIndex((event) => event.event === "ready") + 1,
);

const problem = {
  type: "urn:marimo:problem:not-found",
  title: "Not found",
  status: 404,
};

/** A transport the host refuses. */
const refused: NotebookTransport = async () => {
  throw new StreamStopped("Not found", { cause: problem });
};

/** A transport whose connections deliver an event and drop after `uptime`. */
function dropsAfter(uptime: number) {
  let opened = 0;
  const transport: NotebookTransport = (_attempt, { open, message }) => {
    opened += 1;
    open();
    message({ event: "marimo", data: {}, id: String(opened) });
    return new Promise((resolve) => setTimeout(resolve, uptime));
  };
  return { transport, opened: () => opened };
}

/** A transport whose one connection never ends, driven by the test. */
function held() {
  let attempt: Parameters<NotebookTransport>[0] | undefined;
  let handlers: Parameters<NotebookTransport>[1] | undefined;
  const transport: NotebookTransport = (given, reported) => {
    attempt = given;
    handlers = reported;
    return new Promise(() => undefined);
  };
  const opened = async () => {
    await vi.waitFor(() => expect(handlers).toBeDefined());
    return { signal: attempt!.signal, ...handlers! };
  };
  return { transport, opened };
}

function collect<K extends string>(events: NotebookEvents<K>) {
  const received: NotebookEvent<K>[] = [];
  events.subscribe((event) => received.push(event));
  return received;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("NotebookEvents", () => {
  it("delivers a snapshot through ready and records the cursor", async () => {
    const { transport, attempts } = scripted(throughReady);
    const events = new NotebookEvents(transport, {
      types: ["cell", "document"],
    });
    const received = collect(events);
    await vi.waitFor(() => expect(events.cursor).toBe("95"));
    expect(received.map((event) => event.event)).toMatchInlineSnapshot(`
      [
        "cell",
        "cell",
        "cell",
        "document",
        "ready",
      ]
    `);
    expect(attempts).toEqual([null]);
    expect(events.status).toBe("connected");
    events.close();
    await expect(events.closed).resolves.toBeUndefined();
    expect(events.status).toBe("closed");
  });

  it("narrows events to the requested kinds", () => {
    const events = new NotebookEvents(scripted().transport, {
      types: ["cell", "marimo"],
    });
    events.close();
    expectTypeOf<NotebookEvent<"cell" | "marimo">["event"]>().toEqualTypeOf<
      "cell" | "cell.removed" | "ready" | "reset" | "marimo"
    >();
    events.subscribe((event) => {
      if (event.event === "cell") {
        expectTypeOf(event.data.cell.id).toBeString();
      } else if (event.event === "marimo") {
        expectTypeOf(event.data).toBeUnknown();
      }
    });
  });

  it("resumes from its cursor after the connection drops", async () => {
    vi.useFakeTimers();
    const { transport, attempts } = scripted(snapshot, []);
    const statuses: string[] = [];
    const events = new NotebookEvents(transport, {
      onStatus: (status) => statuses.push(status),
    });
    await vi.advanceTimersByTimeAsync(2000);
    expect(attempts).toEqual([null, "97"]);
    expect(statuses).toMatchInlineSnapshot(`
      [
        "connected",
        "connecting",
        "connected",
      ]
    `);
    events.close();
  });

  it("gives up on a host that drops each connection soon after opening", async () => {
    vi.useFakeTimers();
    const { transport, opened } = dropsAfter(1000);
    const events = new NotebookEvents(transport, { types: ["marimo"] });
    await vi.advanceTimersByTimeAsync(200_000);
    expect(events.status).toBe("closed");
    expect(opened()).toBe(11);
    await expect(events.closed).rejects.toThrow("could not be reached");
  });

  it("keeps following a host whose connections stay up", async () => {
    vi.useFakeTimers();
    const { transport, opened } = dropsAfter(5000);
    const events = new NotebookEvents(transport, { types: ["marimo"] });
    await vi.advanceTimersByTimeAsync(200_000);
    expect(events.status).not.toBe("closed");
    expect(opened()).toBeGreaterThan(11);
    events.close();
  });

  it("ignores a connection that reports after its attempt ended", async () => {
    let late: Parameters<NotebookTransport>[1] | undefined;
    const transport: NotebookTransport = async (_attempt, handlers) => {
      late ??= handlers;
    };
    const statuses: string[] = [];
    const events = new NotebookEvents(transport, {
      types: ["marimo"],
      onStatus: (status) => statuses.push(status),
    });
    const received = collect(events);
    await vi.waitFor(() => expect(late).toBeDefined());
    // Let the attempt end. The stream is then waiting to reconnect.
    await new Promise((resolve) => setTimeout(resolve, 10));
    late?.open();
    late?.message({ event: "marimo", data: {}, id: "9" });
    expect(statuses).toEqual([]);
    expect(received).toEqual([]);
    expect(events.cursor).toBeNull();
    events.close();
  });

  it("delivers reset before replacing an interrupted snapshot", async () => {
    vi.useFakeTimers();
    const { transport, attempts } = scripted(
      throughReady.slice(0, 4),
      throughReady,
    );
    const events = new NotebookEvents(transport, { types: ["cell"] });
    const received = collect(events);
    await vi.advanceTimersByTimeAsync(2000);
    expect(events.cursor).toBe("95");
    expect(attempts).toEqual([null, null]);
    expect(received.map((event) => event.event)).toMatchInlineSnapshot(`
      [
        "cell",
        "cell",
        "cell",
        "reset",
        "cell",
        "cell",
        "cell",
        "ready",
      ]
    `);
    events.close();
  });

  it("clears the cursor on reset and keeps it across unknown kinds", async () => {
    vi.useFakeTimers();
    const { transport, attempts } = scripted(
      [...throughReady, { event: "future", id: "98", data: {} }],
      [
        { event: "marimo", id: "99", data: { marimo: { op: "x" } } },
        { event: "reset", data: {} },
      ],
    );
    const events = new NotebookEvents(transport, { types: ["marimo"] });
    const received = collect(events);
    await vi.advanceTimersByTimeAsync(2000);
    expect(attempts).toEqual([null, "98"]);
    expect(events.cursor).toBeNull();
    expect(received).toMatchInlineSnapshot(`
      [
        {
          "data": {},
          "event": "ready",
          "id": "95",
        },
        {
          "data": {
            "marimo": {
              "op": "x",
            },
          },
          "event": "marimo",
          "id": "99",
        },
        {
          "data": {},
          "event": "reset",
        },
      ]
    `);
    events.close();
  });

  it("stops with the error its transport rejects with", async () => {
    vi.useFakeTimers();
    const attempts: (string | null)[] = [];
    const transport: NotebookTransport = async ({ cursor }, handlers) => {
      attempts.push(cursor);
      handlers.open();
      if (attempts.length === 1) {
        handlers.message({ event: "ready", id: "41", data: {} });
        return;
      }
      handlers.message({ event: "marimo", id: "42", data: { marimo: {} } });
      throw new StreamStopped("Shutting down", { cause: "MARIMO_SHUTDOWN" });
    };
    const events = new NotebookEvents(transport, { types: ["marimo"] });
    const received = collect(events);
    await vi.advanceTimersByTimeAsync(2000);
    const error = await events.closed.catch((error: unknown) => error);
    expect(attempts).toEqual([null, "41"]);
    expect(received.map((event) => event.event)).toEqual(["ready", "marimo"]);
    expect(error).toBeInstanceOf(StreamStopped);
    expect((error as StreamStopped).cause).toBe("MARIMO_SHUTDOWN");
    expect(events.status).toBe("closed");
  });

  it("tells subscribers why it stopped, unless they left or closed it", async () => {
    const stopped = new NotebookEvents(refused);
    const subscribed = vi.fn();
    const left = vi.fn();
    stopped.subscribe(() => undefined, subscribed);
    stopped.subscribe(() => undefined, left)();
    await stopped.closed.catch(() => undefined);
    const late = vi.fn();
    stopped.subscribe(() => undefined, late);
    const closed = new NotebookEvents(scripted().transport);
    const closing = vi.fn();
    closed.subscribe(() => undefined, closing);
    closed.close();
    await closed.closed;
    expect(subscribed.mock.calls).toEqual([
      [expect.objectContaining({ cause: problem })],
    ]);
    expect(left).not.toHaveBeenCalled();
    expect(late).not.toHaveBeenCalled();
    expect(closing).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "owns each registration of a shared event callback (onError: %s)",
    async (withError) => {
      const { transport, opened } = held();
      const events = new NotebookEvents(transport, { types: ["marimo"] });
      const received = vi.fn();
      const leave = events.subscribe(received, withError ? vi.fn() : undefined);
      events.subscribe(received);
      leave();
      leave();
      (await opened()).message({ event: "marimo", data: {}, id: "1" });
      events.close();
      expect(received.mock.calls).toEqual([
        [{ event: "marimo", data: {}, id: "1" }],
      ]);
    },
  );

  it("owns each registration of a shared error callback", async () => {
    const events = new NotebookEvents(refused);
    const failed = vi.fn();
    const leave = events.subscribe(vi.fn(), failed);
    events.subscribe(vi.fn(), failed);
    leave();
    events.subscribe(vi.fn(), failed);
    leave();
    const error = await events.closed.catch((error: unknown) => error);
    expect(failed.mock.calls).toEqual([[error], [error]]);
  });

  it.each(["status", "error"])(
    "honors unsubscribe during terminal %s delivery",
    async (during) => {
      let leave = () => {};
      const events = new NotebookEvents(refused, {
        onStatus: (status) => {
          if (status === "closed" && during === "status") {
            leave();
          }
        },
      });
      events.subscribe(vi.fn(), () => {
        if (during === "error") {
          leave();
        }
      });
      const removed = vi.fn();
      const remaining = vi.fn();
      leave = events.subscribe(vi.fn(), removed);
      events.subscribe(vi.fn(), remaining);
      const error = await events.closed.catch((error: unknown) => error);
      expect(removed.mock.calls).toEqual([]);
      expect(remaining.mock.calls).toEqual([[error]]);
    },
  );

  it("reports a throwing error callback and still notifies other subscribers", async () => {
    const queued = vi.fn();
    vi.stubGlobal("queueMicrotask", queued);
    const thrown = new Error("subscriber failed");
    const events = new NotebookEvents(refused);
    events.subscribe(vi.fn(), () => {
      throw thrown;
    });
    const remaining = vi.fn();
    events.subscribe(vi.fn(), remaining);
    const error = await events.closed.catch((error: unknown) => error);
    expect(remaining.mock.calls).toEqual([[error]]);
    expect(queued.mock.calls).toEqual([[expect.any(Function)]]);
    expect(queued.mock.calls[0][0]).toThrow(thrown);
  });

  it("ends its connection without notifying error subscribers when aborted", async () => {
    const controller = new AbortController();
    const { transport, opened } = held();
    const events = new NotebookEvents(transport, { signal: controller.signal });
    const failed = vi.fn();
    events.subscribe(vi.fn(), failed);
    const { signal } = await opened();
    controller.abort();
    await expect(events.closed).resolves.toBeUndefined();
    expect(signal.aborted).toBe(true);
    expect(failed.mock.calls).toEqual([]);
  });

  it.each([
    { event: "cell", data: { cell: { id: "c1" } } },
    { event: "reset", data: 5 },
  ])("stops on an invalid protocol event: %j", async (message) => {
    const events = new NotebookEvents(scripted([message]).transport);
    await expect(events.closed).rejects.toThrow(/invalid (cell|reset) event/);
  });

  it("stops a connection that delivers a message that is not an event", async () => {
    const { transport, opened } = held();
    const events = new NotebookEvents(transport);
    const { signal, message } = await opened();
    // A transport reports messages from its own listeners, so the stream
    // stops without its help.
    message(null);
    const error = await events.closed.catch((error: unknown) => error);
    expect((error as StreamStopped).message).toContain("not an event");
    expect((error as StreamStopped).cause).toBeInstanceOf(z.ZodError);
    expect(signal.aborted).toBe(true);
  });

  it("ignores a connection that reports after the stream closed", async () => {
    const { transport, opened } = held();
    const events = new NotebookEvents(transport);
    const received = collect(events);
    const { open, message } = await opened();
    events.close();
    open();
    message({ event: "ready", id: "1", data: {} });
    expect(events.status).toBe("closed");
    expect(events.cursor).toBeNull();
    expect(received).toEqual([]);
  });

  it("iterates until the caller leaves the loop", async () => {
    const events = new NotebookEvents(scripted(throughReady).transport, {
      types: ["cell"],
    });
    const names: string[] = [];
    for await (const event of events) {
      names.push(event.event);
      if (event.event === "ready") {
        break;
      }
    }
    expect(names).toMatchInlineSnapshot(`
      [
        "cell",
        "cell",
        "cell",
        "ready",
      ]
    `);
    expect(events.status).toBe("closed");
    await expect(events.closed).resolves.toBeUndefined();
  });

  it("wakes each pending read when events arrive", async () => {
    const { transport, opened } = held();
    const events = new NotebookEvents(transport, { types: ["marimo"] });
    const iterator = events[Symbol.asyncIterator]();
    const first = iterator.next();
    const second = iterator.next();
    const { message } = await opened();
    message({ event: "marimo", id: "1", data: {} });
    message({ event: "marimo", id: "2", data: {} });
    expect((await first).value).toMatchObject({ id: "1" });
    expect((await second).value).toMatchObject({ id: "2" });
    await iterator.return?.();
    expect(events.status).toBe("closed");
  });
});
