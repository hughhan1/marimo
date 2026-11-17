/* Copyright 2026 Marimo. All rights reserved. */
import type { components } from "@marimo-team/marimo-api";
import type { Inputs, Outputs, OutputTypes } from "#holon-ui";

export type schemas = components["schemas"];
/** A tool's arguments as the page passes them; the network layer names the notebook. */
type ToolInput<K extends keyof Inputs["marimo"]> = Omit<
  Inputs["marimo"][K],
  "notebook_id"
>;
export type AiCompletionRequest = schemas["AiCompletionRequest"];
export type BaseResponse = Outputs["marimo"]["files_open"];
export type CellConfig = schemas["CellConfig"];
/**
 * The status of a cell.
 *
 * queued: queued by the kernel.
 * running: currently executing.
 * idle: not running.
 * disabled-transitively: disabled because an ancestor was disabled.
 */
export type RuntimeState = schemas["CellNotification"]["status"];
export type CodeCompletionRequest = ToolInput<"kernel_code_autocomplete">;
export type AutoExportAsIPYNBRequest = ToolInput<"export_auto_export_ipynb">;
export type AutoExportAsMarkdownRequest =
  ToolInput<"export_auto_export_markdown">;
export type ExportAsHTMLRequest = ToolInput<"export_auto_export_html">;
export type ExportAsMarkdownRequest = ToolInput<"export_markdown">;
export type MarkdownExportFlavor = NonNullable<
  ExportAsMarkdownRequest["flavor"]
>;
export type ExportAsIPYNBRequest = ToolInput<"export_ipynb">;
export type ExportAsScriptRequest = ToolInput<"export_script">;
export type ExportAsPDFRequest = ToolInput<"export_pdf">;
export type ExportAvailabilityResponse =
  Outputs["marimo"]["export_availability"];
export type InstallExportRequirementsRequest =
  ToolInput<"export_requirements_install">;
export type UpdateCellOutputsRequest = ToolInput<"export_update_cell_outputs">;

export interface ExportedFile<T extends BlobPart = BlobPart> {
  contents: T;
  filename: string;
  mediaType: string;
}

export type FileCopyRequest = ToolInput<"files_copy">;
export type FileCopyResponse = Outputs["marimo"]["files_copy"];
export type FileCreateRequest = ToolInput<"files_create">;
export type FileCreateResponse = Outputs["marimo"]["files_create"];
export type FileDeleteRequest = ToolInput<"files_delete">;
export type FileDeleteResponse = Outputs["marimo"]["files_delete"];
export type FileDetailsRequest = ToolInput<"files_file_details">;
export type FileDetailsResponse = Outputs["marimo"]["files_file_details"];
export type FileInfo = OutputTypes["marimo"]["files_copy"]["FileInfo"];
export type FileListRequest = ToolInput<"files_list_files">;
export type FileListResponse = Outputs["marimo"]["files_list_files"];
export type FileRoot = OutputTypes["marimo"]["files_roots"]["FileRoot"];
export type FileRootsResponse = Outputs["marimo"]["files_roots"];
export type FileSearchRequest = ToolInput<"files_search">;
export type FileSearchResponse = Outputs["marimo"]["files_search"];
export type FileMoveRequest = ToolInput<"files_move">;
export type FileMoveResponse = Outputs["marimo"]["files_move"];
export type FileUpdateRequest = ToolInput<"files_update">;
export type FileUpdateResponse = Outputs["marimo"]["files_update"];
export type FormatCellsRequest = ToolInput<"kernel_format">;
export type FormatResponse = Outputs["marimo"]["kernel_format"];
export type InvokeFunctionRequest = ToolInput<"kernel_function_call">;
export type InstallPackagesRequest =
  ToolInput<"kernel_install_missing_packages">;
export type AddPackageRequest = ToolInput<"packages_add">;
export type RemovePackageRequest = ToolInput<"packages_remove">;
export type ListPackagesResponse = Outputs["marimo"]["packages_list"];
export type DependencyTreeResponse = Outputs["marimo"]["packages_tree"];
export type DependencyTreeNode =
  OutputTypes["marimo"]["packages_tree"]["DependencyTreeNode"];

export type SandboxRequest = ToolInput<"packages_sandbox">;
export type SandboxResponse = Outputs["marimo"]["packages_manifest"];
export type UpdateManifestRequest = ToolInput<"packages_manifest">;
export type SyncSandboxResponse = Outputs["marimo"]["packages_sync"];
export type PackageOperationResponse = Outputs["marimo"]["packages_add"];
export type InstantiateNotebookRequest = ToolInput<"kernel_instantiate">;
export type MarimoConfig = schemas["MarimoConfig"];
export type MarimoFile = Outputs["marimo"]["home_tutorial_open"];
export type ListSecretKeysRequest = ToolInput<"secrets_keys">;
export type CreateSecretRequest = ToolInput<"secrets_create">;
export type PreviewDatasetColumnRequest =
  ToolInput<"datasources_preview_column">;
export type PreviewSQLTableRequest = ToolInput<"datasources_preview_sql_table">;
export type ListSQLTablesRequest =
  ToolInput<"datasources_preview_sql_table_list">;
export type ListSQLSchemasRequest =
  ToolInput<"datasources_preview_sql_schema_list">;
export type ListDataSourceConnectionRequest =
  ToolInput<"datasources_preview_datasource_connection">;
export type DiscoverDataSourcesRequest = ToolInput<"datasources_discover">;
export type ValidateSQLRequest = ToolInput<"sql_validate">;
export type DebugCellRequest = ToolInput<"kernel_pdb_pm">;
export type SetBreakpointsRequest = ToolInput<"kernel_pdb_breakpoints">;
export type ReadCodeResponse = Outputs["marimo"]["kernel_read_code"];
export type RecentFilesResponse = Outputs["marimo"]["home_recent_files"];
export type RenameNotebookRequest = ToolInput<"kernel_rename">;
export type ExecuteScratchpadRequest = ToolInput<"kernel_scratchpad_run">;
export type SaveAppConfigurationRequest = ToolInput<"kernel_save_app_config">;
export type SaveLayoutRequest = ToolInput<"kernel_save_layout">;
export type CopyNotebookRequest = ToolInput<"kernel_copy">;
export type SaveUserConfigurationRequest = ToolInput<"kernel_save_user_config">;
/**
 * Client-side shape for creating a file/directory/notebook. The HTTP
 * transport uploads `file` as the body of `/api/files/upload`; the WASM bridge
 * base64-encodes it and crosses the JS<->Py boundary as JSON.
 */
export interface FileCreateInput {
  path: string;
  type: "file" | "directory" | "notebook";
  name: string;
  file?: Blob;
}
export type UpdateUIElementRequest = schemas["UpdateUIElementRequest"];
export type ModelRequest = ToolInput<"kernel_set_model_value">;
export type UpdateUserConfigRequest = schemas["UpdateUserConfigRequest"];
export type ShutdownSessionRequest = Inputs["marimo"]["home_shutdown_session"];
export type Snippet =
  OutputTypes["marimo"]["documentation_snippets"]["Snippet"];
export type SnippetSection =
  OutputTypes["marimo"]["documentation_snippets"]["SnippetSection"];
export type Snippets = Outputs["marimo"]["documentation_snippets"];
export type StdinRequest = ToolInput<"kernel_stdin">;
export type SuccessResponse = Outputs["marimo"]["cache_clear"];
export type UpdateUIElementValuesRequest =
  ToolInput<"kernel_set_ui_element_value">;
export type UsageResponse = Outputs["marimo"]["usage"];
export type EnvironmentInfo = Outputs["marimo"]["environment"];
export type WorkspaceFilesRequest = ToolInput<"home_workspace_files">;
export type WorkspaceFilesResponse = Outputs["marimo"]["home_workspace_files"];
export type RunningNotebooksResponse =
  Outputs["marimo"]["home_running_notebooks"];
export type OpenTutorialRequest = ToolInput<"home_tutorial_open">;
export type TutorialId = OpenTutorialRequest["tutorialId"];
export type InvokeAiToolRequest = ToolInput<"ai_invoke_tool">;
export type InvokeAiToolResponse = Outputs["marimo"]["ai_invoke_tool"];
export type ChatOptions = schemas["ChatOptions"];
export type ClearCacheRequest = ToolInput<"cache_clear">;
export type GetCacheInfoRequest = ToolInput<"cache_info">;
export type LspHealthResponse = Outputs["marimo"]["lsp_health"];
export type LspRestartRequest = ToolInput<"lsp_restart">;
export type LspRestartResponse = Outputs["marimo"]["lsp_restart"];
export type LspServerHealth =
  OutputTypes["marimo"]["lsp_health"]["LspServerHealth"];

export type StorageListEntriesRequest = ToolInput<"storage_list_entries">;
export type StorageDownloadRequest = ToolInput<"storage_download">;

/**
 * Requests sent to the BE during run/edit mode.
 */
export interface RunRequests {
  sendComponentValues: (request: UpdateUIElementValuesRequest) => Promise<null>;
  sendModelValue: (request: ModelRequest) => Promise<null>;
  sendInstantiate: (request: InstantiateNotebookRequest) => Promise<null>;
  sendFunctionRequest: (request: InvokeFunctionRequest) => Promise<null>;
}

/**
 * Requests sent to the BE during edit mode.
 */
export interface EditRequests {
  sendRename: (request: RenameNotebookRequest) => Promise<null>;
  /**
   * Write the notebook's layout beside it. The host protocol carries no
   * layout yet, so this stays a request to the host serving the page.
   */
  saveLayout: (request: SaveLayoutRequest) => Promise<null>;
  sendCopy: (request: CopyNotebookRequest) => Promise<null>;
  sendStdin: (request: StdinRequest) => Promise<null>;
  sendRunScratchpad: (request: ExecuteScratchpadRequest) => Promise<null>;
  sendShutdown: () => Promise<null>;
  sendFormat: (request: FormatCellsRequest) => Promise<FormatResponse>;
  sendCodeCompletionRequest: (request: CodeCompletionRequest) => Promise<null>;
  saveUserConfig: (request: SaveUserConfigurationRequest) => Promise<null>;
  saveAppConfig: (request: SaveAppConfigurationRequest) => Promise<null>;
  sendRestart: () => Promise<null>;
  sendInstallMissingPackages: (
    request: InstallPackagesRequest,
  ) => Promise<null>;
  readCode: () => Promise<{ contents: string }>;
  readSnippets: () => Promise<Snippets>;
  previewDatasetColumn: (request: PreviewDatasetColumnRequest) => Promise<null>;
  previewSQLTable: (request: PreviewSQLTableRequest) => Promise<null>;
  previewSQLTableList: (request: ListSQLTablesRequest) => Promise<null>;
  previewSQLSchemaList: (request: ListSQLSchemasRequest) => Promise<null>;
  previewDataSourceConnection: (
    request: ListDataSourceConnectionRequest,
  ) => Promise<null>;
  discoverDataSources: (request: DiscoverDataSourcesRequest) => Promise<null>;
  validateSQL: (request: ValidateSQLRequest) => Promise<null>;
  openFile: (request: { path: string; lineNumber?: number }) => Promise<null>;
  getUsageStats: () => Promise<UsageResponse>;
  getEnvironmentInfo: () => Promise<EnvironmentInfo>;
  // Debugger
  sendPdb: (request: DebugCellRequest) => Promise<null>;
  sendSetBreakpoints: (request: SetBreakpointsRequest) => Promise<null>;
  // File explorer requests
  getFileRoots: () => Promise<FileRootsResponse>;
  sendListFiles: (request: FileListRequest) => Promise<FileListResponse>;
  sendSearchFiles: (request: FileSearchRequest) => Promise<FileSearchResponse>;
  sendCreateFileOrFolder: (
    request: FileCreateInput,
  ) => Promise<FileCreateResponse>;
  sendDeleteFileOrFolder: (
    request: FileDeleteRequest,
  ) => Promise<FileDeleteResponse>;
  sendCopyFileOrFolder: (request: FileCopyRequest) => Promise<FileCopyResponse>;
  sendRenameFileOrFolder: (
    request: FileMoveRequest,
  ) => Promise<FileMoveResponse>;
  sendUpdateFile: (request: FileUpdateRequest) => Promise<FileUpdateResponse>;
  sendFileDetails: (
    request: FileDetailsRequest,
  ) => Promise<FileDetailsResponse>;
  // Homepage requests
  openTutorial: (request: OpenTutorialRequest) => Promise<MarimoFile>;
  getRecentFiles: () => Promise<RecentFilesResponse>;
  getWorkspaceFiles: (
    request: WorkspaceFilesRequest,
  ) => Promise<WorkspaceFilesResponse>;
  getRunningNotebooks: () => Promise<RunningNotebooksResponse>;
  shutdownSession: (
    request: ShutdownSessionRequest,
  ) => Promise<RunningNotebooksResponse>;
  // Export requests
  getExportAvailability: () => Promise<ExportAvailabilityResponse>;
  installExportRequirements: (
    request: InstallExportRequirementsRequest,
  ) => Promise<ExportAvailabilityResponse>;
  exportAsHTML: (request: ExportAsHTMLRequest) => Promise<ExportedFile<string>>;
  exportAsIPYNB: (
    request: ExportAsIPYNBRequest,
  ) => Promise<ExportedFile<string>>;
  exportAsMarkdown: (
    request: ExportAsMarkdownRequest,
  ) => Promise<ExportedFile<string>>;
  exportAsScript: (
    request: ExportAsScriptRequest,
  ) => Promise<ExportedFile<string>>;
  exportAsPDF: (request: ExportAsPDFRequest) => Promise<ExportedFile<Blob>>;
  autoExportAsHTML: (request: ExportAsHTMLRequest) => Promise<null>;
  autoExportAsMarkdown: (request: AutoExportAsMarkdownRequest) => Promise<null>;
  autoExportAsIPYNB: (request: AutoExportAsIPYNBRequest) => Promise<null>;
  updateCellOutputs: (request: UpdateCellOutputsRequest) => Promise<null>;
  getSandbox: (request: SandboxRequest) => Promise<SandboxResponse>;
  updateManifest: (request: UpdateManifestRequest) => Promise<SandboxResponse>;
  syncSandbox: (request: SandboxRequest) => Promise<SyncSandboxResponse>;
  // Package requests
  getPackageList: () => Promise<ListPackagesResponse>;
  getDependencyTree: () => Promise<DependencyTreeResponse>;
  addPackage: (request: AddPackageRequest) => Promise<PackageOperationResponse>;
  removePackage: (
    request: RemovePackageRequest,
  ) => Promise<PackageOperationResponse>;
  // Secrets requests
  listSecretKeys: (request: ListSecretKeysRequest) => Promise<null>;
  writeSecret: (request: CreateSecretRequest) => Promise<null>;
  // AI Tool requests
  invokeAiTool: (request: InvokeAiToolRequest) => Promise<InvokeAiToolResponse>;
  // Cache requests
  clearCache: () => Promise<null>;
  getCacheInfo: () => Promise<null>;
  // Storage requests
  listStorageEntries: (request: StorageListEntriesRequest) => Promise<null>;
  downloadStorage: (request: StorageDownloadRequest) => Promise<null>;
}

export type RequestKey = keyof (EditRequests & RunRequests);
