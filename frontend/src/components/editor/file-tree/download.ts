/* Copyright 2026 Marimo. All rights reserved. */

import { callMarimo, repliedFile } from "@/core/network/holon";
import { getRequestClient } from "@/core/network/requests";
import { isWasm } from "@/core/wasm/utils";
import { deserializeBlob } from "@/utils/blob";
import { downloadBlob } from "@/utils/download";
import { type Base64String, base64ToDataURL } from "@/utils/json/base64";

/**
 * Download a workspace file by path.
 *
 * Over HTTP the server replies with a download target, which the runtime
 * fetches and checks before it is saved. In WASM there is no HTTP server;
 * contents come over the pyodide bridge as base64.
 */
export async function downloadFile(path: string, name: string): Promise<void> {
  if (!isWasm()) {
    const file = await repliedFile(
      await callMarimo((tools) => tools.files_download({ path })),
      name,
    );
    downloadBlob(file.contents, name);
    return;
  }

  const details = await getRequestClient().sendFileDetails({ path });
  if (details.isBase64 && details.contents) {
    const blob = deserializeBlob(
      base64ToDataURL(
        details.contents as Base64String,
        details.mimeType || "application/octet-stream",
      ),
    );
    downloadBlob(blob, name);
  } else {
    downloadBlob(
      new Blob([details.contents || ""], {
        type: details.mimeType || "application/octet-stream",
      }),
      name,
    );
  }
}
