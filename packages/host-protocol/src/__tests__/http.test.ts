/* Copyright 2026 Marimo. All rights reserved. */

import { Effect, Layer, Stream } from "effect";
import { HttpApiBuilder, HttpApiClient } from "effect/http-api";
import { FetchHttpClient, HttpRouter, HttpServer } from "effect/http";
import { afterAll, describe, expect, it, vi } from "vitest";
import * as Host from "../Host.ts";
import * as Notebook from "../Notebook.ts";
import * as S from "../Schemas.ts";

const events = vi.fn(
  HttpApiBuilder.handler(Host.Api, "Notebook", "events", () =>
    Effect.succeed(
      Stream.make(Notebook.Event.make({ event: "ready", id: "98", data: {} })),
    ),
  ),
);
const host = HttpApiBuilder.group(Host.Api, "Host", (handlers) =>
  handlers.handle("events", () => Effect.succeed(Stream.empty)),
);
const notebook = HttpApiBuilder.group(Host.Api, "Notebook", (handlers) =>
  handlers.handle("events", events),
);
const admit = Layer.succeed(
  Host.HostToken,
  Host.HostToken.of({ hostToken: (httpEffect) => httpEffect }),
);
const app = HttpRouter.toWebHandler(
  HttpApiBuilder.layer(Host.Api).pipe(
    Layer.provide(
      Layer.mergeAll(host, notebook).pipe(Layer.provideMerge(admit)),
    ),
    Layer.provide(HttpServer.layerServices),
  ),
  { disableLogger: true },
);
afterAll(() => app.dispose());

function request(path: string, init?: RequestInit) {
  return app.handler(new Request(`http://localhost${path}`, init));
}

describe("native HTTP streams", () => {
  it("decodes event selection and encodes the protocol SSE envelope", async () => {
    const response = await request(
      "/notebooks/n/events?types=ready&run_id=run",
      {
        headers: { "Last-Event-ID": "97", "Marimo-Attachment-Name": "Test" },
      },
    );
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(await response.text()).toBe("id: 98\nevent: ready\ndata: {}\n\n");
    expect(events.mock.lastCall?.[0]).toMatchObject({
      params: { notebookId: "n" },
      query: { types: "ready", runId: "run" },
      headers: { cursor: "97", attachmentName: "Test" },
    });
  });

  it("uses the same API for a typed HTTP client", async () => {
    const events = await Effect.runPromise(
      Effect.gen(function* () {
        const client = yield* HttpApiClient.make(Host.Api, {
          baseUrl: "http://localhost",
        });
        const stream = yield* client.Notebook.events({
          params: { notebookId: S.NotebookId.make("n") },
          query: {},
          headers: {},
        });
        return yield* Stream.runCollect(stream);
      }).pipe(
        Effect.provide(FetchHttpClient.layer),
        Effect.provideService(FetchHttpClient.Fetch, (input, init) =>
          app.handler(new Request(input, init)),
        ),
      ),
    );
    expect(events).toEqual([{ event: "ready", id: "98", data: {} }]);
  });
});
