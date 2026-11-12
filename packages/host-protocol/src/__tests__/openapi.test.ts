/* Copyright 2026 Marimo. All rights reserved. */

import { OpenApi } from "effect/http-api";
import { expect, it } from "vitest";
import * as Analysis from "../experimental/Analysis.ts";
import * as Host from "../Host.ts";

const { referencePolicy } = Host;
const stable = OpenApi.fromApi(Host.Api, { referencePolicy });
const experimental = OpenApi.fromApi(Analysis.Api, { referencePolicy });
const schemas = stable.components.schemas;

it("closes tools' arguments and keeps other objects open", () => {
  const closed = Object.entries(schemas)
    .filter(([, schema]) => schema.additionalProperties === false)
    .map(([name]) => name);
  expect(closed.toSorted()).toEqual([
    "CancelRunRequest",
    "CatalogEventsRequest",
    "CellConfigInput",
    "CellInput",
    "CreateNotebookRequest",
    "DeleteCell",
    "EditRequest",
    "ExecuteRequest",
    "InputReplyRequest",
    "NotebookEventsRequest",
    "NotebookRequest",
    "PutCell",
    "RequestedCell",
    "RunRequest",
    "SaveRequest",
    "SetOrder",
    "StartRuntimeRequest",
    "UpdateNotebookRequest",
  ]);
  expect(schemas.ReadyEvent.properties).toMatchObject({
    data: { type: "object", additionalProperties: false },
  });
});

it("puts descriptions beside their schemas and names each problem", () => {
  expect(JSON.stringify(stable)).not.toContain('"allOf"');
  expect(
    Object.keys(schemas).filter((name) => name.endsWith("Encoded")),
  ).toEqual([]);
  expect(schemas.NotFoundProblem.required).toEqual(
    expect.arrayContaining(["type", "status", "title"]),
  );
  expect(schemas.NotFoundProblem.properties).toMatchObject({
    status: { type: "integer", enum: [404], default: 404 },
    title: { type: "string", default: "Not found" },
  });
  for (const [name, schema] of Object.entries(schemas)) {
    if (
      !name.endsWith("Problem") ||
      name === "Problem" ||
      name === "UnknownProblem"
    ) {
      continue;
    }
    expect(schema.properties).toMatchObject({
      status: { type: "integer", default: expect.any(Number) },
      title: { type: "string", default: expect.any(String) },
    });
  }
});

it("lists each tool with its arguments, result and problems", () => {
  const tools = (stable as { "x-tools"?: Array<Record<string, unknown>> })[
    "x-tools"
  ];
  const save = tools?.find((tool) => tool.name === "notebook.save");
  expect(save).toMatchObject({
    title: "Write the document to the host's store",
    inputSchema: { $ref: "#/components/schemas/SaveRequest" },
    outputSchema: { $ref: "#/components/schemas/EditResponse" },
  });
  expect(save?.problems).toContainEqual({
    $ref: "#/components/schemas/PreconditionFailedProblem",
  });
  expect(
    tools?.find((tool) => tool.name === "notebook.delete"),
  ).not.toHaveProperty("outputSchema");
});

it("lists each event with its arguments and payload, and no HTTP paths", () => {
  expect(stable.paths).toEqual({});
  const events = (stable as { "x-events"?: Array<Record<string, unknown>> })[
    "x-events"
  ];
  expect(events).toEqual([
    expect.objectContaining({
      name: "catalog",
      inputSchema: { $ref: "#/components/schemas/CatalogEventsRequest" },
      payloadSchema: { $ref: "#/components/schemas/CatalogStreamEvent" },
    }),
    expect.objectContaining({
      name: "notebook",
      inputSchema: { $ref: "#/components/schemas/NotebookEventsRequest" },
      payloadSchema: { $ref: "#/components/schemas/NotebookStreamEvent" },
    }),
  ]);
});

it("adds experimental features without changing the stable protocol", () => {
  expect(experimental.paths).toEqual(stable.paths);
  expect(experimental["x-tools" as keyof typeof experimental]).toEqual(
    stable["x-tools" as keyof typeof stable],
  );
  const extended = experimental.components.schemas;
  for (const [name, schema] of Object.entries(schemas)) {
    if (
      name === "Event" ||
      name === "ObjectType" ||
      name === "CatalogStreamEvent" ||
      name === "NotebookStreamEvent"
    ) {
      continue;
    }
    expect(extended[name]).toEqual(schema);
  }
  expect(new Set(extended.ObjectType.enum as string[])).toEqual(
    new Set([...(schemas.ObjectType.enum as string[]), "analysis"]),
  );
  expect(
    Object.keys(extended)
      .filter((name) => !(name in schemas))
      .toSorted(),
  ).toEqual(["Analysis", "AnalysisEvent", "AnalysisMessage", "CellAnalysis"]);
});
