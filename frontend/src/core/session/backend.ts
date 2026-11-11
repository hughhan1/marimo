/* Copyright 2026 Marimo. All rights reserved. */

/** The backend the editor follows, chosen once for its mode. */

import { NotebookClient, StreamStopped } from "@marimo-team/client";
import type { NotebookId } from "@marimo-team/client/protocol";
import type { NotebookBackend } from "../notebook/replica";
import { PyodideBridge } from "../wasm/bridge";
import { isWasm } from "../wasm/utils";
import { getMarimo } from "../network/holon";
import {
  type SessionArguments,
  type SubscribeSession,
  sessionEvents,
  withHeaders,
} from "../transports/server";
import {
  createStaticNotebookBackend,
  emptySnapshot,
  type StaticNotebookSnapshot,
  startWithSnapshot,
} from "../transports/static";

/** How the editor follows its notebook. */
export interface EditorBackendOptions {
  static: boolean;
  /** The notebook the page was served with, shown until the host answers. */
  snapshot: StaticNotebookSnapshot | null;
  /** What the page subscribes to its session with, read before each connection. */
  sessionArguments: () => SessionArguments;
  /** The runtime's headers, read before each request. */
  headers: () => Record<string, string>;
  /** The host URL of a notebook the stream reported. */
  notebookUrl: (notebookId: NotebookId) => URL;
  /** Wait until the host can accept the first connection. */
  waitUntilReady: () => Promise<void>;
  /** Whether the host is reachable before an explicit reconnect. */
  checkHealth: () => Promise<boolean>;
}

/**
 * Read the session's arguments once the host has started.
 *
 * Every connection waits for the same startup. Once it settles, even by
 * giving up, connections read the route directly. An explicit reconnect
 * checks the host's health instead.
 */
function afterStartup({
  sessionArguments,
  waitUntilReady,
}: Pick<EditorBackendOptions, "sessionArguments" | "waitUntilReady">) {
  let startup: Promise<void> | undefined;
  let settled = false;
  return async () => {
    if (!settled) {
      startup ??= waitUntilReady().finally(() => {
        settled = true;
      });
      try {
        await startup;
      } catch (error) {
        throw new StreamStopped("Healthy connection never made", {
          cause: error,
        });
      }
    }
    return sessionArguments();
  };
}

/** The server's `session` event, through the editor's MCP client. */
const subscribeSession: SubscribeSession = async (args, { signal }) => {
  const { ext } = await getMarimo();
  return ext.events.subscribe("session", args, { signal, delivery: "push" });
};

/**
 * Follow the notebook running in the WASM worker.
 *
 * The worker's backend exists once the worker has started, so the first
 * connection creates it. Commands are only for a notebook its stream reported.
 */
function wasm(): NotebookBackend {
  let backend: Promise<NotebookBackend> | undefined;
  let started: NotebookBackend | undefined;
  return {
    transport: async (attempt, handlers) => {
      backend ??= PyodideBridge.INSTANCE.createNotebookBackend();
      try {
        started = await backend;
      } catch (error) {
        throw new StreamStopped("The WASM notebook did not start", {
          cause: error,
        });
      }
      return started.transport(attempt, handlers);
    },
    notebook: (id) => {
      if (started === undefined) {
        throw new Error("The WASM notebook has not started");
      }
      return started.notebook(id);
    },
  };
}

/** Choose the backend for the editor's notebook. */
export function createEditorBackend(
  options: EditorBackendOptions,
): NotebookBackend {
  if (options.static) {
    return createStaticNotebookBackend(
      options.snapshot ?? emptySnapshot("static"),
    );
  }
  const live = isWasm() ? wasm() : server(options);
  return options.snapshot ? startWithSnapshot(options.snapshot, live) : live;
}

/** Follow the notebook on the server, through its `session` event. */
function server(options: EditorBackendOptions): NotebookBackend {
  return {
    transport: sessionEvents(afterStartup(options), subscribeSession),
    notebook: (id) =>
      new NotebookClient(options.notebookUrl(id), {
        fetch: withHeaders(options.headers),
      }),
    checkHealth: options.checkHealth,
  };
}
