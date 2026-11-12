// holon-generated-apps-v1 app=client
import type { ToolClient, EndpointBinding, CreateOptions } from "holon-sh/runtime";
export { ToolCallError } from "holon-sh/runtime";
export type HostOutputNotebook = { "id": string; "name": string; "operations": Array<string>; "path": (string | null); "project_id": string; "updated_at": (string | null); };
export type HostInputIdempotencyKeyNotebookId = { "idempotency_key": string; "notebook_id": string; };
export type HostInputPutCell = { "after"?: string; "base_etag": (string | null); "before"?: string; "cell": HostInputCellInput; "kind": ("put_cell"); };
export type HostInputCellInput = { "code": string; "config"?: HostInputCellConfigInput; "id": string; "name"?: string; };
export type HostInputCellConfigInput = { "column"?: (number | null); "disabled"?: boolean; "expand_output"?: boolean; "hide_code"?: boolean; };
export type HostInputDeleteCell = { "base_etag": string; "id": string; "kind": ("delete_cell"); };
export type HostInputSetOrder = { "base_etag": string; "kind": ("set_order"); "order": Array<string>; };
export type HostOutputEditResponse = { "cells": { [key: string]: (HostOutputCellVersion | null); }; "document": HostOutputDocument; };
export type HostOutputCellVersion = { "code_fingerprint": string; "etag": string; "version": number; };
export type HostOutputDocument = { "app_config": { [key: string]: unknown; }; "app_config_etag": string; "etag": string; "notebook_id": string; "order": Array<string>; "order_etag": string; "saved_etag": (string | null); "version": number; };
export type HostOutputOpenNotebookResponse = { "url": string; };
export type HostOutputRun = { "cells": Array<string>; "completed_at": (string | null); "id": string; "notebook_id": string; "outcome": (("failed" | "interrupted" | "succeeded") | null); "runtime_id": string; "started_at": (string | null); "status": ("completed" | "queued" | "running"); };
export type HostOutputInput = { "execution_id": string; "id": string; "password": boolean; "prompt": string; "seq": number; "status": ("answered" | "cancelled" | "pending"); };
export type HostOutputRuntime = { "id": string; "marimo_version": (string | null); "message": (string | null); "notebook_id": string; "started_at": string; "status": ("failed" | "running" | "starting" | "terminating"); };
export type HostInputRequestedCell = { "etag"?: string; "id": string; };
export interface Inputs {
  "host": {
    "notebook.create": { "idempotency_key": string; "name"?: string; "path"?: string; "project_id": string; };
    "notebook.delete": HostInputIdempotencyKeyNotebookId;
    "notebook.edit": { "changes": Array<(HostInputPutCell | HostInputDeleteCell | HostInputSetOrder)>; "idempotency_key": string; "notebook_id": string; };
    "notebook.open": HostInputIdempotencyKeyNotebookId;
    "notebook.save": { "decisions"?: { [key: string]: string; }; "idempotency_key": string; "if_match"?: (string | null); "notebook_id": string; };
    "notebook.update": { "idempotency_key": string; "name"?: string; "notebook_id": string; "path"?: string; };
    "runtime.cancel": { "idempotency_key": string; "notebook_id": string; "run_id": string; };
    "runtime.execute": { "code": string; "idempotency_key": string; "notebook_id": string; };
    "runtime.input": { "idempotency_key": string; "input_id": string; "notebook_id": string; "value": string; };
    "runtime.interrupt": HostInputIdempotencyKeyNotebookId;
    "runtime.restart": HostInputIdempotencyKeyNotebookId;
    "runtime.run": { "cells": Array<HostInputRequestedCell>; "idempotency_key": string; "notebook_id": string; };
    "runtime.start": { "decisions"?: { [key: string]: string; }; "idempotency_key": string; "notebook_id": string; };
    "runtime.stop": HostInputIdempotencyKeyNotebookId;
  };
}
export interface Outputs {
  "host": {
    "notebook.create": HostOutputNotebook;
    "notebook.delete": {  };
    "notebook.edit": HostOutputEditResponse;
    "notebook.open": HostOutputOpenNotebookResponse;
    "notebook.save": HostOutputEditResponse;
    "notebook.update": HostOutputNotebook;
    "runtime.cancel": HostOutputRun;
    "runtime.execute": HostOutputRun;
    "runtime.input": HostOutputInput;
    "runtime.interrupt": HostOutputRuntime;
    "runtime.restart": HostOutputRuntime;
    "runtime.run": HostOutputRun;
    "runtime.start": HostOutputRuntime;
    "runtime.stop": HostOutputRuntime;
  };
}
export interface InputTypes {
  "host": {
    "notebook.create": {  };
    "notebook.delete": {  };
    "notebook.edit": { "CellConfigInput": HostInputCellConfigInput; "CellInput": HostInputCellInput; "DeleteCell": HostInputDeleteCell; "PutCell": HostInputPutCell; "SetOrder": HostInputSetOrder; };
    "notebook.open": {  };
    "notebook.save": {  };
    "notebook.update": {  };
    "runtime.cancel": {  };
    "runtime.execute": {  };
    "runtime.input": {  };
    "runtime.interrupt": {  };
    "runtime.restart": {  };
    "runtime.run": { "RequestedCell": HostInputRequestedCell; };
    "runtime.start": {  };
    "runtime.stop": {  };
  };
}
export interface OutputTypes {
  "host": {
    "notebook.create": {  };
    "notebook.delete": {  };
    "notebook.edit": { "CellVersion": HostOutputCellVersion; "Document": HostOutputDocument; };
    "notebook.open": {  };
    "notebook.save": { "CellVersion": HostOutputCellVersion; "Document": HostOutputDocument; };
    "notebook.update": {  };
    "runtime.cancel": {  };
    "runtime.execute": {  };
    "runtime.input": {  };
    "runtime.interrupt": {  };
    "runtime.restart": {  };
    "runtime.run": {  };
    "runtime.start": {  };
    "runtime.stop": {  };
  };
}
export interface Resources {
  "host": {
  };
}
export interface EventArguments {
  "host": {
  };
}
export interface EventData {
  "host": {
  };
}
export type Events = { readonly [A in keyof EventArguments]: { [K in keyof EventArguments[A]]: { arguments: EventArguments[A][K]; data: K extends keyof EventData[A] ? EventData[A][K] : unknown } } };

export type Apps = { readonly [A in keyof Inputs]: ToolClient<Inputs[A], Outputs[A], Resources[A], Events[A]> };
export declare const apps: Apps;
export type { EndpointBinding, CreateOptions };
export declare function createApps<K extends keyof Apps>(bindings: { [A in K]: EndpointBinding }, options?: CreateOptions): Pick<Apps,K>;
