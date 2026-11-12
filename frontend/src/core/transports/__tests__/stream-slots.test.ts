/* Copyright 2026 Marimo. All rights reserved. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MAX_STREAMS, type StreamSlots, streamSlots } from "../stream-slots";

/** Channels that deliver each message to the others, as a BroadcastChannel does. */
function bus() {
  const members = new Set<EventTarget>();
  return () => {
    const channel = new EventTarget();
    members.add(channel);
    return Object.assign(channel, {
      postMessage: (data: unknown) => {
        for (const member of members) {
          if (member !== channel) {
            queueMicrotask(() =>
              member.dispatchEvent(new MessageEvent("message", { data })),
            );
          }
        }
      },
    }) as unknown as BroadcastChannel;
  };
}

interface Tab {
  slots: StreamSlots;
  show(): void;
  hide(): void;
}

let clock = 0;
let channel: () => BroadcastChannel;

/** A tab opened now, seen now, then shown or hidden. */
function tab(visible: boolean): Tab {
  clock += 1;
  const document = Object.assign(new EventTarget(), {
    visibilityState: "visible",
  });
  const slots = streamSlots({
    document: document as unknown as Document,
    channel: channel(),
    now: () => clock,
  });
  const set = (state: "visible" | "hidden") => {
    clock += 1;
    document.visibilityState = state;
    document.dispatchEvent(new Event("visibilitychange"));
  };
  if (!visible) {
    set("hidden");
  }
  return { slots, show: () => set("visible"), hide: () => set("hidden") };
}

/** Whether a tab may hold a stream now: whether `hold` settles at once. */
async function holds(tab: Tab): Promise<boolean> {
  const waiting = new AbortController();
  const held = tab.slots.hold(waiting.signal).then(
    () => true,
    () => false,
  );
  await settle();
  waiting.abort();
  return held;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval"] });
  channel = bus();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("streamSlots", () => {
  it("lets the most recently seen tabs hold a stream, however many are visible", async () => {
    const visible = Array.from({ length: MAX_STREAMS + 1 }, () => tab(true));
    await settle();
    const holding = await Promise.all(visible.map(holds));
    expect(holding).toEqual([false, true, true, true, true]);
  });

  it("lets visible tabs and the most recently seen hidden ones hold a stream", async () => {
    const hidden = Array.from({ length: MAX_STREAMS + 1 }, () => tab(false));
    const visible = tab(true);
    await settle();
    expect(await holds(visible)).toBe(true);
    const holding = await Promise.all(hidden.map(holds));
    // The visible tab and the most recently seen hidden ones, up to the limit.
    expect(holding).toEqual([false, false, true, true, true]);
  });

  it("asks a hidden tab to let its stream go once another is seen, and lets it hold again when shown", async () => {
    const tabs = Array.from({ length: MAX_STREAMS }, () => tab(false));
    await settle();
    const oldest = tabs[0];
    const held = await oldest.slots.hold(new AbortController().signal);
    tab(true);
    await settle();
    expect(held.aborted).toBe(true);
    expect(await holds(oldest)).toBe(false);
    oldest.show();
    await settle();
    expect(await holds(oldest)).toBe(true);
  });
});
