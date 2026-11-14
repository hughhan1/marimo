/* Copyright 2026 Marimo. All rights reserved. */

import { NotebookEvents, StreamStopped } from "@marimo-team/client";
import { describe, expect, it, vi } from "vitest";
import {
  type Session,
  type SessionArguments,
  type SessionEvent,
  type SessionState,
  sessionEvents,
} from "../server";

const ARGS: SessionArguments = { session_id: "s1", query_params: "" };

type Follow = Parameters<Session["follow"]>[1];

function fakeSession(states: SessionState[]) {
  const follows: Follow[] = [];
  const pending: ((event: object, eventId: string) => void)[] = [];
  let failure: unknown;
  let gate: Promise<unknown> | undefined;
  const session: Session = {
    read: vi.fn(async () => {
      const state = states.shift();
      if (state === undefined) {
        throw new Error("no state left");
      }
      return state;
    }),
    follow: vi.fn(async (_args, options) => {
      follows.push(options);
      await gate;
      options.onStatus("connected");
      if (failure !== undefined) {
        throw failure;
      }
      const queue: { eventId: string; data: SessionEvent }[] = [];
      let wake = () => undefined as void;
      pending.push((event, eventId) => {
        queue.push({ eventId, data: event as SessionEvent });
        wake();
      });
      return {
        cursor: options.cursor,
        async *[Symbol.asyncIterator]() {
          while (!options.signal.aborted) {
            const event = queue.shift();
            if (event === undefined) {
              await new Promise<void>((resolve) => {
                wake = resolve;
                options.signal.addEventListener("abort", () => resolve(), {
                  once: true,
                });
              });
            } else {
              yield event;
            }
          }
        },
      };
    }),
  };
  let sent = 0;
  return {
    session,
    follows,
    send: (event: object, eventId = `e${++sent}`) =>
      pending.at(-1)?.(event, eventId),
    failWith: (error: unknown) => {
      failure = error;
    },
    holdNext: () => {
      const held = Promise.withResolvers<undefined>();
      gate = held.promise;
      return () => {
        gate = undefined;
        held.resolve(undefined);
      };
    },
  };
}

function collect(events: NotebookEvents<string>) {
  const received: unknown[] = [];
  events.subscribe((event) => received.push(event));
  return received;
}

const ready = { event: "ready", id: "h-1", data: {} };
const reset = { event: "reset", data: {} };

describe("sessionEvents", () => {
  it("delivers the session's state, then follows its changes from the state's cursor", async () => {
    const fake = fakeSession([{ cursor: "c1", events: [reset, ready] }]);
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, fake.session),
      {
        types: ["ready", "reset", "marimo"],
      },
    );
    const received = collect(events);
    await vi.waitFor(() => expect(fake.follows).toHaveLength(1));
    expect(fake.follows[0].cursor).toBe("c1");
    const change = { event: "marimo", data: { marimo: { op: "alert" } } };
    fake.send(change);
    await vi.waitFor(() => expect(received).toEqual([reset, ready, change]));
    expect(events.status).toBe("connected");
    events.close();
  });

  it("stops with the status of the reason a session refused the page, after its last words", async () => {
    const error = {
      event: "marimo",
      data: { marimo: { op: "kernel-startup-error", error: "boom" } },
    };
    const fake = fakeSession([]);
    vi.mocked(fake.session.read).mockResolvedValueOnce({
      cursor: "c1",
      events: [error],
      closed: "MARIMO_KERNEL_STARTUP_ERROR",
    });
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, fake.session),
      {
        types: ["marimo"],
      },
    );
    const received = collect(events);
    const stopped = await events.closed.catch((error: unknown) => error);
    expect(received).toEqual([error]);
    expect(stopped).toBeInstanceOf(StreamStopped);
    expect((stopped as StreamStopped).cause).toMatchObject({
      reason: "Failed to start kernel sandbox",
    });
    expect(fake.session.follow).not.toHaveBeenCalled();
  });

  it.each([
    [{ code: -32024, message: "Forbidden" }, "not authorized"],
    [
      {
        code: -32000,
        message: "MARIMO_KIOSK_NOT_ALLOWED",
        data: { reason: "MARIMO_KIOSK_NOT_ALLOWED" },
      },
      "kiosk mode is not available for this session",
    ],
    [new Error("Lost the connection"), "kernel not found"],
  ])(
    "stops when the session ends or refuses the subscription (%o)",
    async (failure, reason) => {
      const fake = fakeSession([{ cursor: "c1", events: [reset, ready] }]);
      fake.failWith(failure);
      const events = new NotebookEvents(
        sessionEvents(async () => ARGS, fake.session),
      );
      const stopped = await events.closed.catch((error: unknown) => error);
      expect(stopped).toBeInstanceOf(StreamStopped);
      expect((stopped as StreamStopped).cause).toMatchObject({
        state: "CLOSED",
        reason,
      });
    },
  );

  it("shows it is connecting while the client reconnects, and whole again once it is back", async () => {
    const fake = fakeSession([{ cursor: "c1", events: [reset, ready] }]);
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, fake.session),
      { types: ["ready", "reset"] },
    );
    const received = collect(events);
    await vi.waitFor(() => expect(events.status).toBe("connected"));
    fake.follows[0].onStatus("reconnecting");
    expect(events.status).toBe("connecting");
    fake.follows[0].onStatus("connected");
    expect(events.status).toBe("connected");
    expect(received).toHaveLength(3);
    expect(received.at(-1)).toMatchObject({ event: "ready" });
    expect(fake.session.follow).toHaveBeenCalledTimes(1);
    expect(fake.session.read).toHaveBeenCalledTimes(1);
    events.close();
  });

  it("reads the state again after the server skipped changes, and follows from its cursor", async () => {
    const fake = fakeSession([
      { cursor: "c1", events: [reset, ready] },
      { cursor: "c9", events: [reset, { ...ready, id: "h-9" }] },
    ]);
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, fake.session),
      {
        types: ["ready", "reset"],
      },
    );
    const received = collect(events);
    await vi.waitFor(() => expect(fake.follows).toHaveLength(1));
    fake.follows[0].onStatus("gap");
    await vi.waitFor(() => expect(fake.follows).toHaveLength(2));
    expect(fake.follows[0].signal.aborted).toBe(true);
    expect(fake.follows[1].cursor).toBe("c9");
    expect(received).toEqual([reset, ready, reset, { ...ready, id: "h-9" }]);
    events.close();
  });

  it("keeps following until its next subscription is confirmed, passing on each change once", async () => {
    let focused = false;
    const view = Object.assign(new EventTarget(), {
      document: { hasFocus: () => focused } as Document,
    });
    const fake = fakeSession([{ cursor: "c1", events: [reset, ready] }]);
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, fake.session, view),
      { types: ["ready", "reset", "marimo"] },
    );
    const received = collect(events);
    await vi.waitFor(() => expect(received).toHaveLength(2));
    const confirm = fake.holdNext();
    focused = true;
    view.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => expect(fake.follows).toHaveLength(2));
    const change = { event: "marimo", data: { marimo: { op: "alert" } } };
    fake.send(change, "e1");
    await vi.waitFor(() => expect(received).toHaveLength(3));
    expect(fake.follows[0].signal.aborted).toBe(false);
    confirm();
    await vi.waitFor(() => expect(fake.follows[0].signal.aborted).toBe(true));
    fake.send(change, "e1");
    fake.send(change, "e2");
    await vi.waitFor(() => expect(received).toHaveLength(4));
    expect(received).toEqual([reset, ready, change, change]);
    events.close();
  });

  it("polls unless its window has focus, where it is pushed, resuming from its cursor", async () => {
    let focused = false;
    const view = Object.assign(new EventTarget(), {
      document: { hasFocus: () => focused } as Document,
    });
    const fake = fakeSession([{ cursor: "c1", events: [reset, ready] }]);
    const events = new NotebookEvents(
      sessionEvents(async () => ARGS, fake.session, view),
    );
    await vi.waitFor(() => expect(fake.follows).toHaveLength(1));
    focused = true;
    view.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => expect(fake.follows).toHaveLength(2));
    focused = false;
    view.dispatchEvent(new Event("blur"));
    await vi.waitFor(() => expect(fake.follows).toHaveLength(3));
    expect(
      fake.follows.map(({ delivery, cursor }) => [delivery, cursor]),
    ).toEqual([
      ["poll", "c1"],
      ["push", "c1"],
      ["poll", "c1"],
    ]);
    expect(fake.session.read).toHaveBeenCalledTimes(1);
    events.close();
  });
});
