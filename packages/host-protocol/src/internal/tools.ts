/* Copyright 2026 Marimo. All rights reserved. */

import { Context, Option, type Schema, SchemaAST } from "effect";
import { Tool } from "effect/ai";

const reference = (schema: Schema.Top) => {
  const name = SchemaAST.resolveIdentifier(schema.ast);
  if (name === undefined) {
    throw new Error(
      "A tool's arguments, result and problems are named schemas",
    );
  }
  return { $ref: `#/components/schemas/${name}` };
};

const returns = (tool: Tool.Any) => tool.successSchema.ast._tag !== "Void";

const problems = (tool: Tool.Any): ReadonlyArray<Schema.Top> =>
  "members" in tool.failureSchema
    ? (tool.failureSchema.members as ReadonlyArray<Schema.Top>)
    : [tool.failureSchema];

/**
 * An MCP event a host serves: what a subscription names in its arguments, and
 * what each occurrence carries.
 */
export interface Subscription {
  readonly name: string;
  readonly description: string;
  readonly arguments: Schema.Top;
  readonly payload: Schema.Top;
}

/** The schemas the tools name, for the document's components. */
export const toolSchemas = (tools: ReadonlyArray<Tool.Any>) => [
  ...new Set(
    tools.flatMap((tool) => [
      tool.parametersSchema,
      ...(returns(tool) ? [tool.successSchema] : []),
    ]),
  ),
];

/**
 * The tools as the document's `x-tools`: each one's name, title and
 * description, its arguments and result by reference, and the problems it
 * can answer with.
 */
export const toolEntries = (tools: ReadonlyArray<Tool.Any>) =>
  tools.map((tool) => ({
    name: tool.name,
    title: Option.getOrUndefined(
      Context.getOption(tool.annotations, Tool.Title),
    ),
    description: Tool.getDescription(tool),
    inputSchema: reference(tool.parametersSchema),
    ...(returns(tool) ? { outputSchema: reference(tool.successSchema) } : {}),
    problems: problems(tool).map(reference),
  }));

/** The schemas the events name, for the document's components. */
export const eventSchemas = (events: ReadonlyArray<Subscription>) =>
  events.flatMap((event) => [event.arguments, event.payload]);

/**
 * The events as the document's `x-events`: each one's name and description,
 * and its arguments and payload by reference.
 */
export const eventEntries = (events: ReadonlyArray<Subscription>) =>
  events.map((event) => ({
    name: event.name,
    description: event.description,
    inputSchema: reference(event.arguments),
    payloadSchema: reference(event.payload),
  }));
