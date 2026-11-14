/* Copyright 2026 Marimo. All rights reserved. */

import {
  ConnectionClosedReason,
  ConnectionState,
  type ConnectionStatus,
} from "../notebook/replica";
import { Logger } from "@/utils/Logger";

/**
 * The status the editor shows once its session refused or ended it.
 *
 * Keep the server's reasons in sync with marimo/_server/codes.py.
 */
export function closedStatus(reason: string | undefined): ConnectionStatus & {
  reason: string;
} {
  const closed = (
    reason: string,
    code: ConnectionClosedReason = ConnectionClosedReason.KERNEL_DISCONNECTED,
  ) => ({ state: ConnectionState.CLOSED, code, reason }) as const;
  switch (reason) {
    case "MARIMO_UNAUTHORIZED":
      return closed("not authorized");
    case "MARIMO_KIOSK_NOT_ALLOWED":
      return closed("kiosk mode is not available for this session");
    case "MARIMO_KERNEL_STARTUP_ERROR":
      return closed(
        "Failed to start kernel sandbox",
        ConnectionClosedReason.KERNEL_STARTUP_ERROR,
      );
    case undefined:
    case "MARIMO_NO_FILE_KEY":
    case "MARIMO_NO_SESSION_ID":
    case "MARIMO_NO_SESSION":
    case "MARIMO_SHUTDOWN":
      return closed("kernel not found");
    default:
      Logger.warn("Unrecognized connection close reason", reason);
      return closed("kernel not found");
  }
}
