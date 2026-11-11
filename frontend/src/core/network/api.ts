/* Copyright 2026 Marimo. All rights reserved. */

import { HTTPError } from "../../utils/errors";
import { Logger } from "../../utils/Logger";
import { Strings } from "../../utils/strings";
import { getRuntimeManager } from "../runtime/config";

function getBaseUriWithoutQueryParams(): string {
  // Remove query params and hash
  const url = getRuntimeManager().httpURL;
  url.search = "";
  url.hash = "";
  return url.toString();
}

/**
 * Wrapper around fetch that adds XSRF token and session ID to the request and
 * strong types. A `Blob` body is sent as it is, anything else as JSON.
 */
export const API = {
  post<REQ, RESP = null>(
    url: string,
    body: REQ,
    opts: {
      headers?: Record<string, string>;
      baseUrl?: string;
      signal?: AbortSignal;
    } = {},
  ): Promise<RESP> {
    const baseUrl = Strings.withTrailingSlash(
      opts.baseUrl ?? getBaseUriWithoutQueryParams(),
    );
    const fullUrl = `${baseUrl}api${url}`;
    const raw = body instanceof Blob;
    return fetch(fullUrl, {
      method: "POST",
      headers: {
        ...(raw ? {} : { "Content-Type": "application/json" }),
        ...API.headers(),
        ...opts.headers,
      },
      body: raw ? body : JSON.stringify(body),
      signal: opts.signal,
    })
      .then(async (response) => {
        const isJson = response.headers
          .get("Content-Type")
          ?.startsWith("application/json");
        if (!response.ok) {
          const errorBody = isJson
            ? await response.json()
            : await response.text();
          throw new HTTPError(response.status, response.statusText, errorBody);
        }
        if (isJson) {
          return response.json() as RESP;
        }
        return response.text() as unknown as RESP;
      })
      .catch((error) => {
        if (error?.name !== "AbortError") {
          Logger.error(`Error requesting ${fullUrl}`, error);
        }
        throw error;
      });
  },
  headers() {
    const runtimeManager = getRuntimeManager();
    return runtimeManager.headers();
  },
};
