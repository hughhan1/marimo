/* Copyright 2026 Marimo. All rights reserved. */

/* oxlint-disable typescript/no-explicit-any */

import { beforeEach, describe, expect, it, vi } from "vitest";
import contract from "../../../../../packages/openapi/holon-contract.json";
import { createNetworkRequests } from "../requests-network";

// Mock dependencies
vi.mock("../../runtime/config", () => ({
  getRuntimeManager: vi.fn(() => ({
    sessionHeaders: vi.fn(() => ({ "X-Test-Header": "test" })),
  })),
}));

const calls = vi.hoisted(() => new Map<string, ReturnType<typeof vi.fn>>());
const tool = vi.hoisted(() => (name: string) => {
  if (!calls.has(name)) {
    calls.set(
      name,
      vi.fn(() =>
        Promise.resolve({
          downloads: [
            { mimeType: "text/plain", blob: async () => new Blob([""]) },
          ],
        }),
      ),
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
  beforeEach(() => {
    vi.clearAllMocks();
    calls.clear();
  });

  describe("special behavior", () => {
    it("exportAsHTML should set assetUrl in dev/test mode", async () => {
      const originalEnv = process.env.NODE_ENV;
      process.env.NODE_ENV = "development";

      const requests = createNetworkRequests();
      await requests.exportAsHTML({} as any);

      expect(tool("export_html")).toHaveBeenCalledWith(
        expect.objectContaining({ assetUrl: window.location.origin }),
      );

      process.env.NODE_ENV = originalEnv;
    });

    it("exportAsPDF should pass preset through to the API", async () => {
      tool("export_pdf").mockResolvedValue({
        downloads: [
          {
            mimeType: "application/pdf",
            blob: async () => new Blob(["%PDF"]),
          },
        ],
      });
      const requests = createNetworkRequests();
      await requests.exportAsPDF({
        webpdf: false,
        preset: "slides",
        includeInputs: false,
      } as any);

      expect(tool("export_pdf")).toHaveBeenCalledWith(
        expect.objectContaining({
          webpdf: false,
          preset: "slides",
          includeInputs: false,
        }),
      );
    });

    it("exportAsScript should request a text export", async () => {
      const requests = createNetworkRequests();
      await requests.exportAsScript({ download: false });

      expect(tool("export_script")).toHaveBeenCalledWith({ download: false });
    });

    it("getEnvironmentInfo should read the environment", async () => {
      const requests = createNetworkRequests();
      await requests.getEnvironmentInfo();

      expect(tool("environment")).toHaveBeenCalled();
    });

    it("getFileRoots should read the file roots", async () => {
      const requests = createNetworkRequests();
      await requests.getFileRoots();

      expect(tool("files_roots")).toHaveBeenCalled();
    });

    it("getExportAvailability should read export availability", async () => {
      const requests = createNetworkRequests();
      await requests.getExportAvailability();

      expect(tool("export_availability")).toHaveBeenCalled();
    });

    it("installExportRequirements should POST the export format", async () => {
      const requests = createNetworkRequests();
      await requests.installExportRequirements({ format: "pdf" });

      expect(tool("export_requirements_install")).toHaveBeenCalledWith({
        format: "pdf",
      });
    });

    it("discoverDataSources should POST to the discovery endpoint", async () => {
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

      expect(tool("packages_list")).toHaveBeenCalled();
      expect(waitForConnectionOpenIfNotebook).toHaveBeenCalled();
      expect(waitForConnectionOpen).not.toHaveBeenCalled();
    });

    it("sendCreateFileOrFolder sends a file with the upload tool", async () => {
      const requests = createNetworkRequests();
      const file = new Blob(["bytes"]);
      await requests.sendCreateFileOrFolder({
        path: "/dir",
        type: "file",
        name: "a.bin",
        file,
      });
      await requests.sendCreateFileOrFolder({
        path: "/dir",
        type: "directory",
        name: "sub",
      });

      expect(tool("files_upload")).toHaveBeenCalledWith({
        path: "/dir",
        name: "a.bin",
        file,
      });
      expect(tool("files_create")).toHaveBeenCalledWith({
        path: "/dir",
        type: "directory",
        name: "sub",
      });
    });

    it("exportAsIPYNB should call the new endpoint as text", async () => {
      const requests = createNetworkRequests();
      await requests.exportAsIPYNB({
        download: false,
      } as any);

      expect(tool("export_ipynb")).toHaveBeenCalledWith(
        expect.objectContaining({ download: false }),
      );
    });
  });
});
