/* Copyright 2026 Marimo. All rights reserved. */
// Generated from packages/host-protocol. Do not edit.
// Regenerate with: pnpm --dir packages/client codegen

import * as Protocol from "./protocol.ts";

/**
 * The host's tools by name: each one's arguments, its result, or `null`
 * when it has none, and the problems it can answer with.
 */
export const tools = {
  /** Create a notebook. */
  "notebook.create": {
    arguments: Protocol.CreateNotebookRequest,
    result: Protocol.Notebook,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
    ],
  },
  /** Delete a notebook. */
  "notebook.delete": {
    arguments: Protocol.NotebookRequest,
    result: null,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
    ],
  },
  /** Change parts of the document. */
  "notebook.edit": {
    arguments: Protocol.EditRequest,
    result: Protocol.EditResponse,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.PreconditionFailedProblem,
    ],
  },
  /** Show a notebook in the host's own interface. */
  "notebook.open": {
    arguments: Protocol.NotebookRequest,
    result: Protocol.OpenNotebookResponse,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
    ],
  },
  /** Answer an execution's pending input. */
  "runtime.input": {
    arguments: Protocol.InputReplyRequest,
    result: Protocol.Input,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
    ],
  },
  /** Interrupt the notebook's runtime. */
  "runtime.interrupt": {
    arguments: Protocol.NotebookRequest,
    result: Protocol.Runtime,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.NoRuntimeProblem,
    ],
  },
  /** Cancel a run. */
  "runtime.cancel": {
    arguments: Protocol.CancelRunRequest,
    result: Protocol.Run,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
    ],
  },
  /** Restart the notebook's kernel. */
  "runtime.restart": {
    arguments: Protocol.NotebookRequest,
    result: Protocol.Runtime,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.NoRuntimeProblem,
    ],
  },
  /** Run notebook cells. */
  "runtime.run": {
    arguments: Protocol.RunRequest,
    result: Protocol.Run,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.NoRuntimeProblem,
      Protocol.PreconditionFailedProblem,
    ],
  },
  /** Execute Python with notebook values. */
  "runtime.execute": {
    arguments: Protocol.ExecuteRequest,
    result: Protocol.Run,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.NoRuntimeProblem,
    ],
  },
  /** Start the notebook's runtime, or find the one it has. */
  "runtime.start": {
    arguments: Protocol.StartRuntimeRequest,
    result: Protocol.Runtime,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.DecisionRequiredProblem,
    ],
  },
  /** Stop the notebook's runtime. */
  "runtime.stop": {
    arguments: Protocol.NotebookRequest,
    result: Protocol.Runtime,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.NoRuntimeProblem,
    ],
  },
  /** Write the document to the host's store. */
  "notebook.save": {
    arguments: Protocol.SaveRequest,
    result: Protocol.EditResponse,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
      Protocol.PreconditionFailedProblem,
      Protocol.DecisionRequiredProblem,
    ],
  },
  /** Rename or move a notebook. */
  "notebook.update": {
    arguments: Protocol.UpdateNotebookRequest,
    result: Protocol.Notebook,
    problems: [
      Protocol.InvalidRequestProblem,
      Protocol.ForbiddenProblem,
      Protocol.NotFoundProblem,
      Protocol.InternalProblem,
      Protocol.ConflictProblem,
      Protocol.UnsupportedOperationProblem,
    ],
  },
} as const;

/** A tool's name, such as `notebook.edit`. */
export type ToolName = keyof typeof tools;

/** The host's events by name: each one's arguments and the events it carries. */
export const events = {
  catalog: {
    arguments: Protocol.CatalogEventsRequest,
    payload: Protocol.CatalogStreamEvent,
  },
  notebook: {
    arguments: Protocol.NotebookEventsRequest,
    payload: Protocol.NotebookStreamEvent,
  },
} as const;

/** An event's name, such as `notebook`. */
export type EventName = keyof typeof events;
