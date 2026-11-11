/* Copyright 2026 Marimo. All rights reserved. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HTTPError } from "@/utils/errors";
import { Logger } from "@/utils/Logger";
import { API } from "../api";

// Mock fetch globally
const mockFetch = vi.fn();
global.fetch = mockFetch;

// Default mock for getRuntimeManager
let baseUrl = "http://localhost:8000";
vi.mock("@/core/runtime/config", () => ({
  getRuntimeManager: () => ({
    get httpURL() {
      return new URL(baseUrl);
    },
    headers: () => ({}),
  }),
}));

describe("API", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    baseUrl = "http://localhost:8000";
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(["before fetch", "during fetch"])(
    "API.post propagates cancellation %s without logging it",
    async (timing) => {
      const log = vi.spyOn(Logger, "error").mockReturnValue(undefined);
      const controller = new AbortController();
      mockFetch.mockImplementation(
        (_url: string, { signal }: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            if (signal?.aborted) {
              reject(signal.reason);
            } else {
              signal?.addEventListener("abort", () => reject(signal.reason), {
                once: true,
              });
            }
          }),
      );
      if (timing === "before fetch") {
        controller.abort();
      }

      const response = API.post("/foo", {}, { signal: controller.signal });
      controller.abort();

      await expect(response).rejects.toBe(controller.signal.reason);
      expect(mockFetch).toHaveBeenCalledWith(
        "http://localhost:8000/api/foo",
        expect.objectContaining({ signal: controller.signal }),
      );
      expect(log).not.toHaveBeenCalled();
    },
  );

  it.each(["application/json", "text/plain"])(
    "API.post propagates an abort while reading %s without logging it",
    async (contentType) => {
      const log = vi.spyOn(Logger, "error").mockReturnValue(undefined);
      const error = new DOMException("Body read aborted", "AbortError");
      const readBody = vi.fn().mockRejectedValue(error);
      mockFetch.mockResolvedValue({
        ok: true,
        headers: new Headers({ "Content-Type": contentType }),
        json: readBody,
        text: readBody,
      });

      await expect(API.post("/foo", {})).rejects.toBe(error);
      expect(readBody).toHaveBeenCalledExactlyOnceWith();
      expect(log).not.toHaveBeenCalled();
    },
  );

  it.each([new TypeError("Failed to fetch"), null, "Request failed"])(
    "API.post logs and preserves non-abort fetch failures: %s",
    async (error) => {
      const log = vi.spyOn(Logger, "error").mockReturnValue(undefined);
      const controller = new AbortController();
      controller.abort();
      mockFetch.mockRejectedValue(error);

      await expect(
        API.post("/foo", {}, { signal: controller.signal }),
      ).rejects.toBe(error);
      expect(log).toHaveBeenCalledExactlyOnceWith(
        "Error requesting http://localhost:8000/api/foo",
        error,
      );
    },
  );

  it("API.post logs HTTP failures and preserves their status and body", async () => {
    const log = vi.spyOn(Logger, "error").mockReturnValue(undefined);
    const body = { detail: "Service unavailable" };
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 503,
        statusText: "Service Unavailable",
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(API.post("/foo", {})).rejects.toMatchObject({
      name: "HTTPError",
      status: 503,
      message: "Service Unavailable",
      cause: body,
    });
    expect(log).toHaveBeenCalledExactlyOnceWith(
      "Error requesting http://localhost:8000/api/foo",
      expect.any(HTTPError),
    );
  });

  it("API.post logs malformed JSON responses", async () => {
    const log = vi.spyOn(Logger, "error").mockReturnValue(undefined);
    mockFetch.mockResolvedValue(
      new Response("invalid json", {
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(API.post("/foo", {})).rejects.toBeInstanceOf(SyntaxError);
    expect(log).toHaveBeenCalledExactlyOnceWith(
      "Error requesting http://localhost:8000/api/foo",
      expect.any(SyntaxError),
    );
  });

  it("API.post calls fetch with POST and correct URL", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      headers: { get: () => "application/json" },
      json: () => Promise.resolve({ ok: true }),
    });
    await API.post("/foo", { bar: 1 });
    expect(mockFetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/foo",
      expect.objectContaining({ method: "POST" }),
    );
  });


  it("API.post handles base URL with path correctly", async () => {
    baseUrl = "http://example.com/e";
    mockFetch.mockResolvedValue({
      ok: true,
      headers: { get: () => "application/json" },
      json: () => Promise.resolve({ ok: true }),
    });
    await API.post("/foo", { bar: 1 });
    expect(mockFetch).toHaveBeenCalledWith(
      "http://example.com/e/api/foo",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("API.post handles base URL with trailing slash correctly", async () => {
    baseUrl = "http://example.com/e/";
    mockFetch.mockResolvedValue({
      ok: true,
      headers: { get: () => "application/json" },
      json: () => Promise.resolve({ ok: true }),
    });
    await API.post("/foo", { bar: 1 });
    expect(mockFetch).toHaveBeenCalledWith(
      "http://example.com/e/api/foo",
      expect.objectContaining({ method: "POST" }),
    );
  });









});
