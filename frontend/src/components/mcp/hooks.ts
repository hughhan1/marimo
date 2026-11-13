/* Copyright 2026 Marimo. All rights reserved. */

import type { Outputs } from "#holon-ui";
import { useState } from "react";
import { callMarimo } from "@/core/network/holon";
import { useAsyncData } from "@/hooks/useAsyncData";
import { toast } from "../ui/use-toast";

export type MCPStatus = Outputs["marimo"]["ai_mcp_status"];
export type MCPRefreshResponse = Outputs["marimo"]["ai_mcp_refresh"];

/**
 * Hook to fetch MCP server status
 */
export function useMCPStatus() {
  return useAsyncData<MCPStatus>(async () => {
    return callMarimo((tools) => tools.ai_mcp_status({}));
  }, []);
}

/**
 * Hook to refresh MCP server configuration
 */
export function useMCPRefresh() {
  const [isRefreshing, setIsRefreshing] = useState(false);

  const refresh = async () => {
    setIsRefreshing(true);
    try {
      await callMarimo((tools) => tools.ai_mcp_refresh({}));
      toast({
        title: "MCP refreshed",
        description: "MCP server configuration has been refreshed successfully",
      });
    } catch (error) {
      toast({
        title: "Refresh failed",
        description:
          error instanceof Error ? error.message : "Failed to refresh MCP",
        variant: "danger",
      });
    } finally {
      setIsRefreshing(false);
    }
  };

  return { refresh, isRefreshing };
}
