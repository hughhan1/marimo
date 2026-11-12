/* Copyright 2026 Marimo. All rights reserved. */

import { Schema, type SchemaRepresentation } from "effect";
import { Tool, Toolkit } from "effect/ai";
import { HttpApi, OpenApi } from "effect/http-api";
import * as S from "./Schemas.ts";
import * as Problems from "./Problems.ts";
import * as P from "./api/Parameters.ts";
import { forGenerators } from "./internal/openapi.ts";
import {
  eventEntries,
  eventSchemas,
  type Subscription,
  toolEntries,
  toolSchemas,
} from "./internal/tools.ts";
import { CatalogEvent, derive } from "./Events.ts";
import { objects, type ObjectKind } from "./Objects.ts";
import * as Notebook from "./Notebook.ts";

export const Event = CatalogEvent;
export type Event = typeof Event.Type;

export const Create = Tool.make("notebook.create", {
  description:
    "Adds an empty notebook to the project. The call carries the\nnotebook's own fields, and the host fills in what it owns. In a\nproject with a `root`, `path` is required and `name` defaults to\nthe filename; if a file already exists at `path`, the host\nanswers `conflict` and leaves it alone. In a project without a\nroot, `name` is required, the host chooses storage, and `path` is\nnull. A call missing the required field is `invalid-request`.\nThe new notebook appears on the catalog stream with this\ncall's key.\n",
  parameters: S.CreateNotebookRequest,
  success: S.Notebook,
  failure: Schema.Union([...Problems.Mutation.members]),
}).annotate(Tool.Title, "Create a notebook");

export const EventsRequest = Schema.Struct({
  types: Schema.optionalKey(P.Types),
  cursor: Schema.optionalKey(P.Cursor),
}).annotate({
  description:
    "Which of the catalog's events a subscription follows, and where it resumes.",
  identifier: "CatalogEventsRequest",
});

/** The catalog's events, for the stream's `CatalogEvent` union. */
export const subscription = <E extends Schema.Top>(
  events: E,
): Subscription => ({
  name: "catalog",
  description:
    "Sends everything the host has, then every change as it happens.\nThe catalog is the host object, its projects, notebooks, runtimes,\nand attachments.\n\nA subscription starts with one event per object the host knows\nabout, parents before children, followed by a `ready` event.\nSnapshot events have no `id`; `ready` and live events have one. The\nmost recent `id` a client has seen is its position. When a\nsubscription ends, the client subscribes again with that position as\n`cursor`. If the host still has the events after that point, it\nsends them with their original ids and timestamps, even before the\nnew `ready`, and then `ready`. If it does not, or the cursor\npredates a loss of host state, it sends `reset`, and the client\ndiscards what it holds and receives the full state again. A client\nalso discards an incomplete snapshot before subscribing again\nwithout a cursor. On `reset` it clears its cursor until the next\n`ready`.\n\n`types` limits the event to some kinds of object without changing\nids or positions. It also filters the host object; `ready` and\n`reset` are always sent. Keep the same filter when resuming a\ncursor.\n",
  arguments: EventsRequest,
  payload: events,
});

/** Every operation a host serves, as MCP tools named by operation id. */
export const Tools = Toolkit.merge(Toolkit.make(Create), Notebook.Tools);

const DESCRIPTION =
  "The contract a marimo host serves so that other programs on the same\nmachine can find it, see what it has, and work with its notebooks. A\nhost is a program that runs marimo notebooks, such as the marimo CLI\nor the VS Code extension. A client is a program that talks to a host\nfrom the outside, such as Pair.\n\nA host writes a small file, the host record, that says where its API\nis and how to authenticate. A client reads that file, subscribes to the\nhost's events to learn what it has, and calls the tools the host\nlists, all over MCP at `/mcp`. There are two events: `catalog` carries\nthe catalog, and `notebook` carries one notebook: its cells and\ndocument, its runtime, and the runs of code against it. Clients ignore\nfields, operation names, event\nnames, and problem types they do not recognize. A host rejects a call\nwith an argument it does not recognize. A change that would\nbreak an existing client requires a new record version and a new base\npath.\n";

/**
 * The API for a table of objects.
 *
 * An extension passes the table with its own rows appended.
 */
export const make = <const Objects extends ReadonlyArray<ObjectKind>>(
  objects: Objects,
) => {
  const derived = derive(objects);
  const events = [
    subscription(derived.CatalogEvent),
    Notebook.subscription(derived.NotebookEvent),
  ];
  const tools = Object.values(Tools.tools);
  return HttpApi.make("Host")
    .annotate(HttpApi.AdditionalSchemas, [
      derived.Event,
      derived.ObjectType,
      S.HostRecord,
      Problems.Problem,
      Problems.UnknownProblem,
      ...toolSchemas(tools),
      ...eventSchemas(events),
    ])
    .annotate(OpenApi.Title, "marimo host protocol")
    .annotate(OpenApi.Version, "1.0.0")
    .annotate(OpenApi.Description, DESCRIPTION)
    .annotate(OpenApi.Transform, (spec) =>
      forGenerators({
        ...spec,
        "x-tools": toolEntries(tools),
        "x-events": eventEntries(events),
      }),
    );
};

export const Api = make(objects);

/**
 * How `OpenApi.fromApi` names the schemas of this protocol.
 *
 * Effect calls the wire schema of a class `<Class>Encoded`, so a problem would
 * be published as `NotFoundProblemEncoded`. With this policy it is published
 * as `NotFoundProblem`, the name of its class.
 *
 * ```ts
 * OpenApi.fromApi(Host.Api, { referencePolicy: Host.referencePolicy });
 * ```
 */
export const referencePolicy: SchemaRepresentation.ReferencePolicy = ({
  identifier,
}) => identifier?.replace(/Encoded$/, "");
