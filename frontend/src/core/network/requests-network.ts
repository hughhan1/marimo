/* Copyright 2026 Marimo. All rights reserved. */

import {
  getDefaultExportFilename,
  getDefaultMarkdownExportFilename,
} from "./export-filename";
import {
  waitForConnectionOpen,
  waitForConnectionOpenIfNotebook,
} from "./connection";
import { withDevAssetUrl } from "./export-asset-url";
import { type Replied, getTools, repliedFile } from "./holon";
import type { EditRequests, RunRequests } from "./types";

/** A text file a tool replied with, named `fallback` when the reply names none. */
async function textFile(reply: Replied, fallback: string) {
  const file = await repliedFile(reply, fallback);
  return { ...file, contents: await file.contents.text() };
}

export function createNetworkRequests(): EditRequests & RunRequests {
  return {
    sendComponentValues: (request) => {
      return getTools()
        .then((tools) => tools.kernel_set_ui_element_value(request))
        .then(() => null);
    },
    sendModelValue: (request) => {
      return getTools()
        .then((tools) => tools.kernel_set_model_value(request))
        .then(() => null);
    },
    sendRestart: () => {
      return getTools()
        .then((tools) => tools.kernel_restart_session({}))
        .then(() => null);
    },
    sendRename: (request) => {
      return getTools()
        .then((tools) => tools.kernel_rename(request))
        .then(() => null);
    },
    sendCopy: (request) => {
      return getTools()
        .then((tools) => tools.kernel_copy(request))
        .then(() => null);
    },
    sendFormat: (request) => {
      return getTools().then((tools) => tools.kernel_format(request));
    },
    sendShutdown: () => {
      return getTools()
        .then((tools) => tools.kernel_shutdown({}))
        .then(() => null);
    },
    sendRunScratchpad: async (request) => {
      await waitForConnectionOpen();
      return getTools()
        .then((tools) => tools.kernel_scratchpad_run(request))
        .then(() => null);
    },
    sendInstantiate: async (request) => {
      await waitForConnectionOpen();
      return getTools()
        .then((tools) => tools.kernel_instantiate(request))
        .then(() => null);
    },
    sendCodeCompletionRequest: async (request) => {
      await waitForConnectionOpen();
      return getTools()
        .then((tools) => tools.kernel_code_autocomplete(request))
        .then(() => null);
    },
    saveUserConfig: (request) => {
      return getTools()
        .then((tools) => tools.kernel_save_user_config(request))
        .then(() => null);
    },
    saveLayout: (request) => {
      return getTools()
        .then((tools) => tools.kernel_save_layout(request))
        .then(() => null);
    },
    saveAppConfig: (request) => {
      return getTools()
        .then((tools) => tools.kernel_save_app_config(request))
        .then(() => null);
    },
    sendFunctionRequest: (request) => {
      return getTools()
        .then((tools) => tools.kernel_function_call(request))
        .then(() => null);
    },
    sendStdin: (request) => {
      return getTools()
        .then((tools) => tools.kernel_stdin(request))
        .then(() => null);
    },
    sendInstallMissingPackages: (request) => {
      return getTools()
        .then((tools) => tools.kernel_install_missing_packages(request))
        .then(() => null);
    },
    readCode: async () => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.kernel_read_code({}));
    },
    readSnippets: async () => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.documentation_snippets({}));
    },
    previewDatasetColumn: (request) => {
      return getTools()
        .then((tools) => tools.datasources_preview_column(request))
        .then(() => null);
    },
    previewSQLTable: (request) => {
      return getTools()
        .then((tools) => tools.datasources_preview_sql_table(request))
        .then(() => null);
    },
    previewSQLTableList: (request) => {
      return getTools()
        .then((tools) => tools.datasources_preview_sql_table_list(request))
        .then(() => null);
    },
    previewSQLSchemaList: (request) => {
      return getTools()
        .then((tools) => tools.datasources_preview_sql_schema_list(request))
        .then(() => null);
    },
    previewDataSourceConnection: (request) => {
      return getTools()
        .then((tools) =>
          tools.datasources_preview_datasource_connection(request),
        )
        .then(() => null);
    },
    discoverDataSources: (request) => {
      return getTools()
        .then((tools) => tools.datasources_discover(request))
        .then(() => null);
    },
    validateSQL: (request) => {
      return getTools()
        .then((tools) => tools.sql_validate(request))
        .then(() => null);
    },
    openFile: async (request) => {
      await waitForConnectionOpen();
      await getTools()
        .then((tools) => tools.files_open(request))
        .then(() => null);
      return null;
    },
    getUsageStats: async () => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.usage({}));
    },
    getEnvironmentInfo: () => {
      return getTools().then((tools) => tools.environment({}));
    },
    sendPdb: (request) => {
      return getTools()
        .then((tools) => tools.kernel_pdb_pm(request))
        .then(() => null);
    },
    sendSetBreakpoints: (request) => {
      return getTools()
        .then((tools) => tools.kernel_pdb_breakpoints(request))
        .then(() => null);
    },
    getFileRoots: async () => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.files_roots({}));
    },
    sendListFiles: async (request) => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.files_list_files(request));
    },
    sendSearchFiles: async (request) => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.files_search(request));
    },
    sendCreateFileOrFolder: async ({ file, ...request }) => {
      await waitForConnectionOpen();
      const tools = await getTools();
      if (!file) {
        return tools.files_create(request);
      }
      const { path, name } = request;
      return tools.files_upload({ path, name, file });
    },
    sendDeleteFileOrFolder: async (request) => {
      return getTools().then((tools) => tools.files_delete(request));
    },
    sendCopyFileOrFolder: async (request) => {
      return getTools().then((tools) => tools.files_copy(request));
    },
    sendRenameFileOrFolder: async (request) => {
      return getTools().then((tools) => tools.files_move(request));
    },
    sendUpdateFile: async (request) => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.files_update(request));
    },
    sendFileDetails: async (request) => {
      await waitForConnectionOpen();
      return getTools().then((tools) => tools.files_file_details(request));
    },
    openTutorial: (request) => {
      return getTools().then((tools) => tools.home_tutorial_open(request));
    },
    getRecentFiles: () => {
      return getTools().then((tools) => tools.home_recent_files({}));
    },
    getWorkspaceFiles: (request) => {
      return getTools().then((tools) => tools.home_workspace_files(request));
    },
    getRunningNotebooks: () => {
      return getTools().then((tools) => tools.home_running_notebooks({}));
    },
    shutdownSession: (request) => {
      return getTools().then((tools) => tools.home_shutdown_session(request));
    },
    getExportAvailability: () => {
      return getTools().then((tools) => tools.export_availability({}));
    },
    installExportRequirements: (request) => {
      return getTools().then((tools) =>
        tools.export_requirements_install(request),
      );
    },
    exportAsHTML: async (request) => {
      return getTools()
        .then((tools) => tools.export_html(withDevAssetUrl(request)))
        .then((reply) => textFile(reply, getDefaultExportFilename("html")));
    },
    exportAsMarkdown: async (request) => {
      return getTools()
        .then((tools) => tools.export_markdown(request))
        .then((reply) =>
          textFile(reply, getDefaultMarkdownExportFilename(request.flavor)),
        );
    },
    exportAsScript: async (request) => {
      return getTools()
        .then((tools) => tools.export_script(request))
        .then((reply) =>
          textFile(reply, getDefaultExportFilename("script.py")),
        );
    },
    exportAsIPYNB: async (request) => {
      return getTools()
        .then((tools) => tools.export_ipynb(request))
        .then((reply) => textFile(reply, getDefaultExportFilename("ipynb")));
    },
    exportAsPDF: async (request) => {
      return getTools()
        .then((tools) => tools.export_pdf(request))
        .then((reply) => repliedFile(reply, getDefaultExportFilename("pdf")));
    },
    autoExportAsHTML: async (request) => {
      return getTools()
        .then((tools) => tools.export_auto_export_html(request))
        .then(() => null);
    },
    autoExportAsMarkdown: async (request) => {
      return getTools()
        .then((tools) => tools.export_auto_export_markdown(request))
        .then(() => null);
    },
    autoExportAsIPYNB: async (request) => {
      return getTools()
        .then((tools) => tools.export_auto_export_ipynb(request))
        .then(() => null);
    },
    updateCellOutputs: async (request) => {
      return getTools()
        .then((tools) => tools.export_update_cell_outputs(request))
        .then(() => null);
    },
    getSandbox: (request) =>
      getTools().then((tools) => tools.packages_sandbox(request)),
    updateManifest: (request) =>
      getTools().then((tools) => tools.packages_manifest(request)),
    syncSandbox: (request) =>
      getTools().then((tools) => tools.packages_sync(request)),
    addPackage: (request) => {
      return getTools().then((tools) => tools.packages_add(request));
    },
    removePackage: (request) => {
      return getTools().then((tools) => tools.packages_remove(request));
    },
    getPackageList: async () => {
      // If the sidebar is already open, it may try to load before the session has been initialized
      await waitForConnectionOpenIfNotebook();
      return getTools().then((tools) => tools.packages_list({}));
    },
    getDependencyTree: async () => {
      // If the sidebar is already open, it may try to load before the session has been initialized
      await waitForConnectionOpenIfNotebook();
      return getTools().then((tools) => tools.packages_tree({}));
    },
    listSecretKeys: async (request) => {
      // If the sidebar is already open, it may try to load before the session has been initialized
      await waitForConnectionOpen();
      return getTools()
        .then((tools) => tools.secrets_keys(request))
        .then(() => null);
    },
    writeSecret: async (request) => {
      return getTools()
        .then((tools) => tools.secrets_create(request))
        .then(() => null);
    },
    invokeAiTool: async (request) => {
      return getTools().then((tools) => tools.ai_invoke_tool(request));
    },
    clearCache: async () => {
      return getTools()
        .then((tools) => tools.cache_clear({}))
        .then(() => null);
    },
    getCacheInfo: async () => {
      return getTools()
        .then((tools) => tools.cache_info({}))
        .then(() => null);
    },
    listStorageEntries: (request) => {
      return getTools()
        .then((tools) => tools.storage_list_entries(request))
        .then(() => null);
    },
    downloadStorage: (request) => {
      return getTools()
        .then((tools) => tools.storage_download(request))
        .then(() => null);
    },
  };
}
