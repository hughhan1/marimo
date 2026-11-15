/* Copyright 2026 Marimo. All rights reserved. */

/* oxlint-disable typescript/no-explicit-any */

import { beforeEach, describe, expect, it, vi } from "vitest";
import contract from "../../../../../packages/openapi/holon-contract.json";
import * as apiModule from "../api";
import { createNetworkRequests } from "../requests-network";

// Mock dependencies
vi.mock("../../runtime/config", () => ({
  getRuntimeManager: vi.fn(() => ({
    sessionHeaders: vi.fn(() => ({ "X-Test-Header": "test" })),
  })),
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof apiModule>("../api");
  return {
    ...actual,
    API: {
      handleExportResponse: vi.fn((response) => response.data),
      handleResponse: vi.fn((response) => response.data),
      handleResponseReturnNull: vi.fn(() => null),
    },
    createClientWithRuntimeManager: vi.fn(),
  };
});

const calls = vi.hoisted(() => new Map<string, ReturnType<typeof vi.fn>>());
const tool = vi.hoisted(() => (name: string) => {
  if (!calls.has(name)) {
    calls.set(
      name,
      vi.fn(() => Promise.resolve({})),
    );
  }
  return calls.get(name)!;
});
vi.mock("../holon", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../holon")>()),
  callMarimo: async <T>(
    call: (tools: Record<string, ReturnType<typeof tool>>) => Promise<T>,
  ) =>
    call(
      Object.fromEntries(
        contract.schema.tools.map(({ name }) => [name, tool(name)]),
      ),
    ),
}));

vi.mock("../connection", () => ({
  isConnectedAtom: { read: vi.fn(() => true) },
  waitForConnectionOpen: vi.fn().mockResolvedValue(undefined),
  waitForConnectionOpenIfNotebook: vi.fn().mockResolvedValue(undefined),
}));

describe("createNetworkRequests", () => {
  let mockClient: any;
  let capturedCalls: Map<string, { hasParams: boolean; endpoint: string }>;

  beforeEach(() => {
    vi.clearAllMocks();
    calls.clear();
    capturedCalls = new Map();

    // Create mock client that captures all calls
    const createMockMethod = (endpoint: string) => {
      return vi.fn((_route: string, options?: any) => {
        const hasParams = options?.params !== undefined;
        capturedCalls.set(endpoint, { hasParams, endpoint });
        return Promise.resolve({ data: null, error: undefined });
      });
    };

    mockClient = {
      POST: createMockMethod("POST"),
      GET: createMockMethod("GET"),
    };

    // Mock createClientWithRuntimeManager to return our mock client
    vi.mocked(apiModule.createClientWithRuntimeManager).mockReturnValue(
      mockClient,
    );
  });

  describe("special behavior", () => {
    it("exportAsHTML should set assetUrl in dev/test mode", async () => {
      const originalEnv = process.env.NODE_ENV;
      process.env.NODE_ENV = "development";

      const requests = createNetworkRequests();
      await requests.exportAsHTML({} as any);

      expect(mockClient.POST).toHaveBeenCalledWith(
        "/api/export/html",
        expect.objectContaining({
          body: expect.objectContaining({
            assetUrl: window.location.origin,
          }),
        }),
      );

      process.env.NODE_ENV = originalEnv;
    });

    it("exportAsPDF should pass preset through to the API", async () => {
      const requests = createNetworkRequests();
      await requests.exportAsPDF({
        webpdf: false,
        preset: "slides",
        includeInputs: false,
      } as any);

      expect(mockClient.POST).toHaveBeenCalledWith(
        "/api/export/pdf",
        expect.objectContaining({
          body: expect.objectContaining({
            webpdf: false,
            preset: "slides",
            includeInputs: false,
          }),
          parseAs: "blob",
        }),
      );
    });

    it("exportAsScript should request a text export", async () => {
      const requests = createNetworkRequests();
      await requests.exportAsScript({ download: false });

      expect(mockClient.POST).toHaveBeenCalledWith(
        "/api/export/script",
        expect.objectContaining({
          body: { download: false },
          parseAs: "text",
        }),
      );
    });

    it("getEnvironmentInfo should GET /api/environment", async () => {
      const requests = createNetworkRequests();
      await requests.getEnvironmentInfo();

      expect(mockClient.GET).toHaveBeenCalledWith("/api/environment");
    });

    it("getFileRoots should call files_roots", async () => {
      const requests = createNetworkRequests();
      await requests.getFileRoots();

      expect(tool("files_roots")).toHaveBeenCalledWith({});
    });

    it("getExportAvailability should call export_availability", async () => {
      const requests = createNetworkRequests();
      await requests.getExportAvailability();

      expect(tool("export_availability")).toHaveBeenCalledWith({});
    });

    it("installExportRequirements should POST the export format", async () => {
      const requests = createNetworkRequests();
      await requests.installExportRequirements({ format: "pdf" });

      expect(tool("export_requirements_install")).toHaveBeenCalledWith({
        format: "pdf",
      });
    });

    it("discoverDataSources should call datasources_discover", async () => {
      const requests = createNetworkRequests();
      const request = { requestId: "discovery-request" } as any;
      await requests.discoverDataSources(request);

      expect(tool("datasources_discover")).toHaveBeenCalledWith(request);
    });

    it("getPackageList should not require a kernel connection", async () => {
      const { waitForConnectionOpen, waitForConnectionOpenIfNotebook } =
        await import("../connection");

      const requests = createNetworkRequests();
      await requests.getPackageList();

      expect(tool("packages_list")).toHaveBeenCalledWith({});
      expect(waitForConnectionOpenIfNotebook).toHaveBeenCalled();
      expect(waitForConnectionOpen).not.toHaveBeenCalled();
    });

    it("exportAsIPYNB should call the new endpoint as text", async () => {
      const requests = createNetworkRequests();
      await requests.exportAsIPYNB({
        download: false,
      } as any);

      expect(mockClient.POST).toHaveBeenCalledWith(
        "/api/export/ipynb",
        expect.objectContaining({
          body: expect.objectContaining({
            download: false,
          }),
          parseAs: "text",
        }),
      );
    });
  });
});
