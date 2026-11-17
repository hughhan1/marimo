/* Copyright 2026 Marimo. All rights reserved. */

export interface RuntimeConfig {
  /**
   * The URL of the runtime server.
   */
  url: string;
  /**
   * The server token for the runtime (Skew protection token)
   */
  readonly serverToken?: string;
  /**
   * The API key or JWT token for the remote backend (used for authentication)
   */
  readonly authToken?: string | null;
  /**
   * Where browsers reach the runtime's MCP endpoint, when a proxy serves it
   * somewhere other than below `url`. A path is relative to the page's origin.
   */
  readonly mcpUrl?: string | null;
}
