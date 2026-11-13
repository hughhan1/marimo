/* Copyright 2026 Marimo. All rights reserved. */

import { ToolCallError } from "#holon-ui";
import { describe, expect, it, vi } from "vitest";
import { HTTPError, prettyError } from "@/utils/errors";
import { callMarimo } from "../holon";

vi.mock("../../runtime/config", () => ({
  getRuntimeManager: () => ({
    getMcpURL: () => new URL("http://localhost/api/mcp"),
    httpURL: new URL("http://localhost"),
    headers: () => ({}),
  }),
}));

function failed(result: ToolCallError["result"]) {
  return callMarimo(async () => {
    throw new ToolCallError("marimo", "kernel_run", result);
  });
}

describe("callMarimo", () => {
  it.each([403, 409])(
    "preserves status %i and the server's explanation",
    async (status) => {
      const problem = {
        type: "https://marimo.io/problems/conflict",
        title: "Refused",
        status,
        detail: "The notebook is read-only",
      };
      const result = {
        resultType: "complete" as const,
        isError: true as const,
        content: [{ type: "text" as const, text: JSON.stringify(problem) }],
      };
      await expect(failed(result)).rejects.toMatchObject({
        status,
        cause: problem,
      });
      await expect(
        failed({ ...result, content: [], structuredContent: problem }),
      ).rejects.toMatchObject({ status, cause: problem });
    },
  );

  it("does not trust a malformed problem's status", async () => {
    const text = JSON.stringify({ status: "403", title: "Refused" });
    const error = await failed({
      resultType: "complete",
      isError: true,
      content: [{ type: "text", text }],
    }).catch((error: unknown) => error);
    expect(error).toBeInstanceOf(HTTPError);
    expect(error).toMatchObject({ status: 500 });
    expect(prettyError(error)).toBe(text);
  });

  it("preserves transport failures", async () => {
    const failure = new TypeError("fetch failed");
    await expect(
      callMarimo(async () => {
        throw failure;
      }),
    ).rejects.toBe(failure);
  });
});
