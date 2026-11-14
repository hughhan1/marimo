/* Copyright 2026 Marimo. All rights reserved. */

/** The backend the editor follows, chosen once for its mode. */

import {
  type CallTool,
  mcp,
  NotebookClient,
  StreamStopped,
} from "@marimo-team/client";
import type { NotebookBackend } from "../notebook/replica";
import { PyodideBridge } from "../wasm/bridge";
import { isWasm } from "../wasm/utils";
import { getMarimo, withHeaders } from "../network/holon";
import {
  type Session,
  type SessionArguments,
  sessionEvents,
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
  /** The editor's MCP endpoint, which serves the notebook tools too. */
  mcpUrl: () => URL;
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

const session: Session = {
  read: async (args, { signal }) => {
    const { tools } = await getMarimo();
    return tools.session_state(args, { signal });
  },
  follow: async (args, options) => {
    const { ext } = await getMarimo();
    return ext.events.subscribe("session", args, options);
  },
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
  // Every notebook shares one client of the editor's tools.
  let send: CallTool | undefined;
  return {
    transport: sessionEvents(afterStartup(options), session),
    notebook: (id) =>
      new NotebookClient(
        id,
        (send ??= mcp(options.mcpUrl(), {
          fetch: withHeaders(options.headers),
        })),
      ),
    checkHealth: options.checkHealth,
  };
}
