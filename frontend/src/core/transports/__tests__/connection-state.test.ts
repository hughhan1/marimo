/* Copyright 2026 Marimo. All rights reserved. */

import { describe, expect, it } from "vitest";
import { WebSocketClosedReason, ConnectionState } from "../../session/types";
import { closedStatus } from "../connection-state";

describe("closedStatus", () => {
  it.each([
    [undefined, "kernel not found"],
    ["MARIMO_UNAUTHORIZED", "not authorized"],
    [
      "MARIMO_KIOSK_NOT_ALLOWED",
      "kiosk mode is not available for this session",
    ],
  ])("%s → closed: %s", (reason, shown) => {
    expect(closedStatus(reason)).toEqual({
      state: ConnectionState.CLOSED,
      code: WebSocketClosedReason.KERNEL_DISCONNECTED,
      reason: shown,
    });
  });

  it("shows a kernel that failed to start as such", () => {
    expect(closedStatus("MARIMO_KERNEL_STARTUP_ERROR")).toMatchObject({
      code: WebSocketClosedReason.KERNEL_STARTUP_ERROR,
    });
  });
});
