/* Copyright 2026 Marimo. All rights reserved. */

import { afterEach, describe, expect, it } from "vitest";
import type { SessionId } from "@/core/kernel/session";
import { RuntimeManager } from "../runtime";

const original = window.location.href;

afterEach(() => {
  window.history.pushState({}, "", original);
});

describe("RuntimeManager.getSessionArguments", () => {
  it("passes runtime parameters and repeated page values to mo.query_params", () => {
    window.history.pushState({}, "", "/?tag=page-one&tag=page-two");
    const runtime = new RuntimeManager({
      url: `${window.location.origin}/nested?proxy_token=secret&tag=base&base=one&base=two`,
    });
    expect(runtime.getSessionArguments("session" as SessionId)).toEqual({
      session_id: "session",
      file: null,
      kiosk: false,
      query_params:
        "proxy_token=secret&base=one&base=two&tag=page-one&tag=page-two",
    });
  });

  it("keeps the page's notebook parameters for the notebook, not the session", () => {
    const notebookParams = new URLSearchParams([
      ["types", "scatter"],
      ["cursor", "page-2"],
      ["run_id", "experiment"],
      ["query_params", "nested=value&other=1"],
      ["tag", "one"],
      ["tag", "two"],
      ["empty", ""],
      ["text", "a+b & café"],
      ["file", "test.py"],
      ["kiosk", "true"],
    ]);
    window.history.pushState(
      {},
      "",
      `/?${notebookParams}&session_id=page-session&access_token=page-token`,
    );
    const runtime = new RuntimeManager({ url: window.location.origin });
    const args = runtime.getSessionArguments("actual-session" as SessionId);

    expect(args).toEqual({
      session_id: "actual-session",
      file: "test.py",
      kiosk: true,
      query_params: notebookParams.toString(),
    });
    expect(JSON.stringify(args)).not.toContain("page-token");
  });
});
