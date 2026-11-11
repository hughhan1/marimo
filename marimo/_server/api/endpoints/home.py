# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo import _loggers
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.files.directory_scanner import DirectoryScanner
from marimo._server.host.lifespan import ensure_host
from marimo._server.models.home import (
    MarimoFile,
    OpenTutorialRequest,
    RecentFilesResponse,
    RunningNotebooksResponse,
    ShutdownSessionRequest,
    WorkspaceFilesRequest,
    WorkspaceFilesResponse,
)
from marimo._server.workspace import (
    count_files,
    flatten_files,
)
from marimo._session.model import ConnectionState, SessionMode
from marimo._tutorials import create_temp_tutorial_file  # type: ignore
from marimo._utils.http import HTTPException, HTTPStatus
from marimo._utils.paths import pretty_path

if TYPE_CHECKING:
    from starlette.requests import Request

MAX_FILES = DirectoryScanner.MAX_FILES

LOGGER = _loggers.marimo_logger()


@tool("home_recent_files", output=RecentFilesResponse)
@requires("edit")
async def read_code(
    *,
    request: Request,
) -> RecentFilesResponse:
    """List the recently opened notebooks."""
    app_state = AppState(request)
    # Pass the workspace's directory to filter and relativize paths
    directory = None
    dir_str = app_state.session_manager.workspace.directory
    if dir_str:
        directory = pathlib.Path(dir_str)
    files = app_state.session_manager.recents.get_recents(directory)
    return RecentFilesResponse(files=files)


@tool(
    "home_workspace_files",
    input=WorkspaceFilesRequest,
    output=WorkspaceFilesResponse,
)
@requires("read")
async def workspace_files(
    *,
    request: Request,
    body: WorkspaceFilesRequest,
) -> WorkspaceFilesResponse:
    """List the workspace's files."""
    app_state = AppState(request)
    session_manager = app_state.session_manager

    if session_manager.mode == SessionMode.RUN:
        from marimo._metadata.opengraph import (
            OpenGraphContext,
            resolve_opengraph_metadata,
        )
        from marimo._server.models.files import FileInfo

        if session_manager.watch:
            # In watched folder mode, refresh the index to include new/removed files since the previous request.
            session_manager.workspace.invalidate()

        base_url = app_state.base_url
        mode = session_manager.mode.value

        def get_files_with_metadata() -> list[FileInfo]:
            files = session_manager.workspace.files
            marimo_files = [
                file for file in flatten_files(files) if file.is_marimo_file
            ]
            result: list[FileInfo] = []
            for file in marimo_files:
                try:
                    resolved_path = session_manager.workspace.resolve(
                        file.path
                    )
                except HTTPException as e:
                    if e.status_code == HTTPStatus.NOT_FOUND:
                        continue
                    raise
                opengraph = None
                if resolved_path is not None:
                    # User-defined OpenGraph generators receive this context for dynamic metadata
                    opengraph = resolve_opengraph_metadata(
                        resolved_path,
                        context=OpenGraphContext(
                            filepath=resolved_path,
                            file_key=file.path,
                            base_url=base_url,
                            mode=mode,
                        ),
                        execute_generator=(
                            session_manager.execute_opengraph_generators
                        ),
                    )
                result.append(
                    FileInfo(
                        id=file.id,
                        path=file.path,
                        name=file.name,
                        is_directory=file.is_directory,
                        is_marimo_file=file.is_marimo_file,
                        last_modified=file.last_modified,
                        children=file.children,
                        opengraph=opengraph,
                    )
                )
            return result

        marimo_files = await asyncio.to_thread(get_files_with_metadata)
        file_count = len(marimo_files)
        has_more = (
            file_count >= MAX_FILES or session_manager.workspace.is_truncated
        )
        return WorkspaceFilesResponse(
            files=marimo_files,
            root=session_manager.workspace.directory or "",
            has_more=has_more,
            file_count=file_count,
        )

    # Both calls are no-ops on workspaces that don't support these
    # capabilities (single-file, fixed-files, empty).
    session_manager.workspace.invalidate()
    session_manager.workspace.set_include_markdown(body.include_markdown)
    root = session_manager.workspace.directory or ""

    # Run file scanning in thread pool to avoid blocking the server
    files = await asyncio.to_thread(lambda: session_manager.workspace.files)

    file_count = count_files(files)
    has_more = (
        file_count >= MAX_FILES or session_manager.workspace.is_truncated
    )

    return WorkspaceFilesResponse(
        files=files,
        root=root,
        has_more=has_more,
        file_count=file_count,
    )


def _get_active_sessions(app_state: AppState) -> list[MarimoFile]:
    """Get list of active sessions with prettified paths."""
    # Get directory from workspace for path relativization
    base_dir = app_state.session_manager.workspace.directory

    files: list[MarimoFile] = []
    for session_id, session in app_state.session_manager.sessions.items():
        state = session.connection_state()
        if state == ConnectionState.OPEN or state == ConnectionState.ORPHANED:
            filename = session.app_file_manager.filename
            basename = os.path.basename(filename) if filename else None
            files.append(
                MarimoFile(
                    name=(basename or "new notebook"),
                    path=pretty_path(filename, base_dir)
                    if filename
                    else session_id,
                    last_modified=0,
                    session_id=session_id,
                    initialization_id=session.initialization_id,
                )
            )
    # These are better in reverse
    return files[::-1]


@tool("home_running_notebooks", output=RunningNotebooksResponse)
@requires("edit")
async def running_notebooks(
    *,
    request: Request,
) -> RunningNotebooksResponse:
    """List the notebooks with a running session."""
    app_state = AppState(request)
    return RunningNotebooksResponse(files=_get_active_sessions(app_state))


@tool(
    "home_shutdown_session",
    input=ShutdownSessionRequest,
    output=RunningNotebooksResponse,
)
@requires("edit")
async def shutdown_session(
    *,
    request: Request,
    body: ShutdownSessionRequest,
) -> RunningNotebooksResponse:
    """Close a session and list the notebooks still running."""
    app_state = AppState(request)
    app_state.session_manager.close_session(body.session_id)
    return RunningNotebooksResponse(files=_get_active_sessions(app_state))


@tool("home_tutorial_open", input=OpenTutorialRequest, output=MarimoFile)
@requires("edit")
async def tutorial(
    *,
    request: Request,
    body: OpenTutorialRequest,
) -> MarimoFile:
    """Create a copy of a tutorial notebook and return its path."""
    # Create a new tutorial file and return the filepath
    temp_dir = tempfile.TemporaryDirectory()
    path = create_temp_tutorial_file(body.tutorial_id, temp_dir)

    import atexit

    atexit.register(temp_dir.cleanup)

    # Register the temp file/directory with the workspace so it can be accessed.
    # Each method is a no-op on workspaces that don't support that capability.
    app_state = AppState(request)
    app_state.session_manager.workspace.register_temp_dir(temp_dir.name)
    app_state.session_manager.workspace.register_allowed_path(
        path.absolute_name
    )
    context = await ensure_host(request.app)
    context.bridge.index.register_open_notebook(path.absolute_name)

    return MarimoFile(
        name=os.path.basename(path.absolute_name),
        path=path.absolute_name,
    )
