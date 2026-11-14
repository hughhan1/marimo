/* Copyright 2026 Marimo. All rights reserved. */

import { describe, expect, it, vi } from "vitest";
import { Logger } from "@/utils/Logger";
import { WebSocketClosedReason, ConnectionState } from "../../session/types";
import { closedStatus } from "../connection-state";

describe("closedStatus", () => {
  it.each([
    [undefined, "kernel not found"],
    ["MARIMO_NO_FILE_KEY", "kernel not found"],
    ["MARIMO_NO_SESSION_ID", "kernel not found"],
    ["MARIMO_NO_SESSION", "kernel not found"],
    ["MARIMO_SHUTDOWN", "kernel not found"],
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

  it("closes on a reason it does not know, and logs it", () => {
    const logger = vi.spyOn(Logger, "warn").mockImplementation(() => {});
    expect(closedStatus("something-else")).toMatchObject({
      state: ConnectionState.CLOSED,
    });
    expect(logger).toHaveBeenCalled();
    logger.mockRestore();
  });
});
