/* Copyright 2026 Marimo. All rights reserved. */

/**
 * Which of a browser's tabs may hold a session stream open.
 *
 * Over HTTP/1.1 a browser opens about six connections to a server and shares
 * them between its tabs, and a tab following its session holds one for as
 * long as it follows it. So with six tabs open, no request gets a connection.
 * The tabs of one origin agree through a `BroadcastChannel` that only the
 * `MAX_STREAMS` most recently seen hold a stream, visible ones first. The
 * others let their streams go, and follow their sessions again once they may.
 */
export const MAX_STREAMS = 4;

const CHANNEL = "marimo-session-streams";
const HEARTBEAT_MS = 5000;
const EXPIRES_MS = 3 * HEARTBEAT_MS;

interface Tab {
  readonly visible: boolean;
  /** When the tab was last shown or focused. */
  readonly seen: number;
  /** When this tab last heard of it. */
  readonly heard: number;
}

type Message =
  | {
      readonly kind: "tab";
      readonly id: string;
      readonly visible: boolean;
      readonly seen: number;
    }
  | { readonly kind: "gone"; readonly id: string };

export interface StreamSlots {
  /**
   * Wait until this tab may hold a stream, then a signal that aborts with
   * `signal` or once the tab must let its stream go.
   */
  hold(signal: AbortSignal): Promise<AbortSignal>;
}

/** The slots of tabs in `document`'s browser, coordinated on `channel`. */
export function streamSlots({
  document = globalThis.document,
  channel = new BroadcastChannel(CHANNEL),
  now = Date.now,
}: {
  document?: Document;
  channel?: BroadcastChannel;
  now?: () => number;
} = {}): StreamSlots {
  // `crypto.randomUUID` exists only in a secure context, and a server reached
  // at a network address over HTTP is not one.
  const id = `${now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  const tabs = new Map<string, Tab>();
  const changed = new Set<() => void>();
  const visible = () => document.visibilityState === "visible";
  let seen = now();

  const post = (message: Message) => {
    // A BroadcastChannel reaches only its own origin and takes no target.
    // oxlint-disable-next-line unicorn/require-post-message-target-origin
    channel.postMessage(message);
  };
  const announce = () => post({ kind: "tab", id, visible: visible(), seen });
  const notify = () => {
    for (const listener of changed) {
      listener();
    }
  };
  /** Whether another tab comes first: visible ones, then the most recently seen. */
  const before = (other: string, tab: Tab) => {
    if (tab.visible !== visible()) {
      return tab.visible;
    }
    return tab.seen === seen ? other > id : tab.seen > seen;
  };
  const allowed = () => {
    const cutoff = now() - EXPIRES_MS;
    const ahead = [...tabs].filter(
      ([other, tab]) => tab.heard >= cutoff && before(other, tab),
    );
    return ahead.length < MAX_STREAMS;
  };

  channel.addEventListener("message", ({ data }: MessageEvent<Message>) => {
    if (data.kind === "gone") {
      tabs.delete(data.id);
    } else {
      const known = tabs.has(data.id);
      tabs.set(data.id, {
        visible: data.visible,
        seen: data.seen,
        heard: now(),
      });
      if (!known) {
        // A tab that just opened learns of this one too.
        announce();
      }
    }
    notify();
  });
  const looked = () => {
    if (visible()) {
      seen = now();
    }
    announce();
    notify();
  };
  document.addEventListener("visibilitychange", looked);
  globalThis.addEventListener?.("focus", looked);
  globalThis.addEventListener?.("pagehide", () => post({ kind: "gone", id }));
  setInterval(() => {
    announce();
    notify();
  }, HEARTBEAT_MS);
  announce();

  return {
    hold: async (signal) => {
      while (!allowed()) {
        await new Promise<void>((resolve, reject) => {
          const done = () => {
            changed.delete(done);
            signal.removeEventListener("abort", done);
            if (signal.aborted) {
              reject(signal.reason);
            } else {
              resolve();
            }
          };
          changed.add(done);
          signal.addEventListener("abort", done, { once: true });
        });
      }
      const lost = new AbortController();
      const check = () => {
        if (!allowed()) {
          changed.delete(check);
          lost.abort(new Error("Another tab needs this tab's connection"));
        }
      };
      changed.add(check);
      signal.addEventListener("abort", () => changed.delete(check), {
        once: true,
      });
      return AbortSignal.any([signal, lost.signal]);
    },
  };
}

/**
 * Whether this page's tabs may share a few connections to its server: unless
 * it was loaded over HTTP/2 or 3, which multiplex. A page loaded from the
 * cache names no protocol.
 */
export function sharesConnections(): boolean {
  const entries = globalThis.performance?.getEntriesByType?.("navigation");
  const [navigation] = (entries ?? []) as PerformanceNavigationTiming[];
  return (
    navigation !== undefined &&
    !["h2", "h3"].includes(navigation.nextHopProtocol)
  );
}

/** Slots that never ask a tab to let its stream go. */
export const unlimited: StreamSlots = { hold: async (signal) => signal };
