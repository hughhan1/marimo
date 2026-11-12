/* Copyright 2026 Marimo. All rights reserved. */

import NodeChildProcess from "node:child_process";
import NodeFs from "node:fs";
import NodeOs from "node:os";
import NodePath from "node:path";
import NodeUrl from "node:url";
import NodeUtil from "node:util";
import { generate, type OpenApiDocument } from "orval";

const root = NodeUrl.fileURLToPath(new URL("../../../", import.meta.url));
const { values: args } = NodeUtil.parseArgs({
  options: {
    check: { type: "boolean", default: false },
    "output-dir": { type: "string" },
  },
});
const source = args["output-dir"] ?? NodePath.join(root, "packages/client/src");
// The host protocol package prints its OpenAPI document, piped in on stdin.
const spec: OpenApiDocument = JSON.parse(NodeFs.readFileSync(0, "utf8"));
const header = [
  "/* Copyright 2026 Marimo. All rights reserved. */",
  "// Generated from packages/host-protocol. Do not edit.",
  "// Regenerate with: pnpm --dir packages/client codegen",
  "\n",
].join("\n");

interface Reference {
  $ref: string;
}

/** A tool as the spec's `x-tools` lists it. */
interface Tool {
  name: string;
  title: string;
  inputSchema: Reference;
  outputSchema?: Reference;
  problems: Reference[];
}

/** The last segment of a local `$ref`, such as `EditRequest`. */
function refName(ref: string | undefined): string {
  const name = ref?.match(/^#\/components\/\w+\/(\w+)$/)?.[1];
  if (name === undefined) {
    throw new Error(`Unsupported reference: ${ref}`);
  }
  return name;
}

/** An event as the spec's `x-events` lists it. */
interface Event {
  name: string;
  inputSchema: Reference;
  payloadSchema: Reference;
}

/** Describe each tool and event as data for the client to call and follow. */
function toolsModule(spec: OpenApiDocument): string {
  const { "x-tools": tools, "x-events": events } = spec as {
    "x-tools"?: Tool[];
    "x-events"?: Event[];
  };
  if (tools === undefined || events === undefined) {
    throw new Error("Host protocol lists no tools or events");
  }
  const entries = tools.map((tool) =>
    [
      `/** ${tool.title}. */`,
      `${JSON.stringify(tool.name)}: {`,
      `arguments: Protocol.${refName(tool.inputSchema.$ref)},`,
      `result: ${tool.outputSchema ? `Protocol.${refName(tool.outputSchema.$ref)}` : "null"},`,
      `problems: [${tool.problems.map((problem) => `Protocol.${refName(problem.$ref)}`).join(", ")}],`,
      "},",
    ].join("\n"),
  );
  return `${header}import * as Protocol from "./protocol.ts";

/**
 * The host's tools by name: each one's arguments, its result, or \`null\`
 * when it has none, and the problems it can answer with.
 */
export const tools = {
${entries.join("\n")}
} as const;

/** A tool's name, such as \`notebook.edit\`. */
export type ToolName = keyof typeof tools;

/** The host's events by name: each one's arguments and the events it carries. */
export const events = {
${events
  .map(
    (event) =>
      `${JSON.stringify(event.name)}: { arguments: Protocol.${refName(event.inputSchema.$ref)}, payload: Protocol.${refName(event.payloadSchema.$ref)} },`,
  )
  .join("\n")}
} as const;

/** An event's name, such as \`notebook\`. */
export type EventName = keyof typeof events;
`;
}

const temporary = NodeFs.mkdtempSync(
  NodePath.join(NodeOs.tmpdir(), "marimo-host-protocol-"),
);
try {
  const schemas = spec.components?.schemas;
  if (!schemas) {
    throw new Error("Host protocol has no component schemas");
  }
  const output = NodePath.join(temporary, "protocol.ts");
  await generate({
    input: {
      target: { ...spec, paths: {}, components: { schemas } },
    },
    output: {
      target: NodePath.join(temporary, "operations.ts"),
      client: "zod",
      schemas: { path: output, type: "zod", mode: "single" },
      override: {
        header: false,
        zod: {
          version: 4,
          generateReusableSchemas: true,
          dateTimeOptions: { offset: true },
        },
      },
    },
  });
  let generated = NodeFs.readFileSync(output, "utf8");
  for (const [name, schema] of Object.entries(schemas)) {
    if (
      typeof schema === "object" &&
      "additionalProperties" in schema &&
      schema.additionalProperties === false
    ) {
      const declaration = new RegExp(
        `(export const ${name} = zod\\s*\\.)object\\(`,
      );
      if (!declaration.test(generated)) {
        throw new Error(`Expected an object schema for ${name}`);
      }
      generated = generated.replace(declaration, "$1strictObject(");
    }
  }
  NodeFs.writeFileSync(output, header + generated);
  const table = NodePath.join(temporary, "tools-table.ts");
  NodeFs.writeFileSync(table, toolsModule(spec));
  NodeChildProcess.execFileSync(
    NodePath.join(root, "node_modules/.bin/oxfmt"),
    ["--config", NodePath.join(root, ".oxfmtrc.json"), output, table],
  );
  if (!args.check) {
    NodeFs.mkdirSync(source, { recursive: true });
  }
  for (const [generated, name] of [
    [output, "protocol.ts"],
    [table, "tools.ts"],
  ] as const) {
    const content = NodeFs.readFileSync(generated, "utf8");
    const destination = NodePath.join(source, name);
    if (args.check) {
      if (
        !NodeFs.existsSync(destination) ||
        NodeFs.readFileSync(destination, "utf8") !== content
      ) {
        throw new Error(
          `${name} is stale; regenerate with the same feature selection and output directory`,
        );
      }
    } else {
      NodeFs.writeFileSync(destination, content);
    }
  }
} finally {
  NodeFs.rmSync(temporary, { recursive: true, force: true });
}
