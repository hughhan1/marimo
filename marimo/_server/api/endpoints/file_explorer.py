# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import msgspec
from holon.ext.files import file_input_schema
from starlette.authentication import requires
from starlette.exceptions import HTTPException

from marimo import _loggers
from marimo._server.api.deps import AppState
from marimo._server.api.tools import FileResult, tool
from marimo._server.files.os_file_system import OSFileSystem
from marimo._server.files.roots import resolve_file_roots
from marimo._server.models.files import (
    FileCopyRequest,
    FileCopyResponse,
    FileCreateRequest,
    FileCreateResponse,
    FileDeleteRequest,
    FileDeleteResponse,
    FileDetailsRequest,
    FileDetailsResponse,
    FileListRequest,
    FileListResponse,
    FileMoveRequest,
    FileMoveResponse,
    FileOpenRequest,
    FileRootsResponse,
    FileSearchRequest,
    FileSearchResponse,
    FileUpdateRequest,
    FileUpdateResponse,
)
from marimo._server.models.models import (
    BaseResponse,
    ErrorResponse,
    SuccessResponse,
)
from marimo._utils.http import HTTPException as MarimoHTTPException
from marimo._utils.mime import guess_mime_type

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()

# Design note on scope and path handling (please read).
#
# Every endpoint below is gated by `@requires("edit")`, so it is reachable only
# in edit mode, not by a run-mode viewer (`read` scope). Edit-mode marimo is
# equivalent to a standard IDE, which allows file traversal and arbitrary file
# read/write. Moreover, edit-mode marimo is, by design, equivalent to
# arbitrary code execution. As such, there is no privilege escalation or
# unexpected file read/write vulnerability in these endpoints.
#
# See marimo's security model and disclosure policy in docs/security.md
# (https://docs.marimo.io/security/) before drafting an advisory for this file.
#
# A brief checklist if you are considering disclosing a vulnerability:
#   - Is unexpected or unintended code execution possible?
#   - Are unauthenticated users allowed unintended environment details?
#   - Are unauthenticated users allowed unintended file system access?
#   - Are unauthenticated users allowed unintended arbitrary code execution?
#
# The marimo team takes security seriously, and we welcome responsible
# disclosure of any vulnerabilities. We are happy to discuss any reports.

# Router for file system endpoints

file_system = OSFileSystem()

# Without a cap an authenticated client could exhaust disk. 1 GiB covers
# normal notebook-data use cases with margin.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


class FileUploadRequest(msgspec.Struct, rename="camel"):
    # The directory to put the file in
    path: str
    # The file's name there
    name: str
    # The file, sent in the call or transferred beside it
    file: Annotated[
        dict[str, Any],
        msgspec.Meta(
            extra_json_schema=file_input_schema(max_size=MAX_UPLOAD_BYTES)
        ),
    ]


@tool("files_roots")
@requires("edit")
def file_roots(*, request: Request) -> FileRootsResponse:
    """List the directories the file browser may start from."""
    app_state = AppState(request)
    primary_root = (
        app_state.session_manager.workspace.directory or file_system.get_root()
    )
    config = app_state.config_manager.get_config().file_browser
    return FileRootsResponse(
        roots=resolve_file_roots(primary_root, config),
    )


@tool("files_list_files")
@requires("edit")
async def list_files(
    *,
    request: Request,
    body: FileListRequest,
) -> FileListResponse:
    """List the files in a directory, the workspace's by default."""
    app_state = AppState(request)
    # Use workspace's directory as default, fall back to cwd.
    # NB. This isn't a security boundary; the workspace is just the initial view
    # for the browser.
    directory = app_state.session_manager.workspace.directory
    root = body.path or directory or file_system.get_root()
    files = await asyncio.to_thread(file_system.list_files, root)
    return FileListResponse(files=files, root=root)


@tool("files_file_details")
@requires("edit")
async def file_details(
    *,
    request: Request,
    body: FileDetailsRequest,
) -> FileDetailsResponse:
    """Describe a file and read its contents, bounded when asked."""
    if body.max_bytes is not None and body.max_bytes < 0:
        raise HTTPException(
            status_code=400,
            detail="maxBytes must be non-negative",
        )
    # Only bound the read when the caller asks for it. The file preview is
    # the sole caller that sets a limit; other consumers read in full.
    return file_system.get_details(body.path, max_bytes=body.max_bytes)


class FileDownloadRequest(msgspec.Struct):
    path: str


@tool("files_download")
@requires("edit")
def download_file(
    *, request: Request, body: FileDownloadRequest
) -> FileResult:
    """Download a file. The reply lists where to fetch its bytes."""
    path = body.path
    app_state = AppState(request)
    server_config = app_state.config_manager.get_config().server
    # A downloads-disabled policy cannot be satisfied by re-authenticating,
    # so it stays a 403 rather than Starlette's 401.
    if server_config.disable_file_downloads:
        raise MarimoHTTPException(
            status_code=403, detail="File downloads are disabled"
        )
    if not path:
        raise MarimoHTTPException(status_code=400, detail="Missing path")
    file_path = Path(path)
    if file_path.is_dir():
        raise MarimoHTTPException(
            status_code=400, detail="Cannot download a directory"
        )
    if not file_path.is_file():
        raise MarimoHTTPException(status_code=404, detail="File not found")
    return FileResult(
        data=file_path,
        media_type=guess_mime_type(file_path.name)
        or "application/octet-stream",
        filename=file_path.name,
    )


@tool("files_create")
@requires("edit")
async def create_file_or_directory(
    *,
    request: Request,
    body: FileCreateRequest,
) -> FileCreateResponse:
    """Create an empty file, a new notebook, or a directory."""
    try:
        info = file_system.create_file_or_directory(
            body.path, body.type, body.name, None
        )
        return FileCreateResponse(success=True, info=info)
    except Exception as e:
        LOGGER.error(f"Error creating file or directory: {e}")
        return FileCreateResponse(success=False, message=str(e))


@tool("files_upload")
@requires("edit")
async def upload_file(
    *,
    request: Request,
    body: FileUploadRequest,
) -> FileCreateResponse:
    """Add a file to the directory `path`, as `name`."""
    app_state = AppState(request)
    try:
        info = await file_system.add_file(
            body.path,
            body.name,
            app_state.state.editor_mcp.files.received(body.file),
        )
        return FileCreateResponse(success=True, info=info)
    except Exception as e:
        LOGGER.error(f"Error uploading file: {e}")
        return FileCreateResponse(success=False, message=str(e))


@tool("files_delete")
@requires("edit")
async def delete_file_or_directory(
    *,
    request: Request,
    body: FileDeleteRequest,
) -> FileDeleteResponse:
    """Delete a file or directory."""
    try:
        file_system.get_info(body.path)
        success = file_system.delete_file_or_directory(body.path)
        return FileDeleteResponse(success=success)
    except Exception as e:
        LOGGER.error(f"Error deleting file or directory: {e}")
        return FileDeleteResponse(success=False, message=str(e))


@tool("files_copy")
@requires("edit")
async def copy_file_or_directory(
    *,
    request: Request,
    body: FileCopyRequest,
) -> FileCopyResponse:
    """Copy a file or directory to a new path."""
    try:
        file_system.get_info(body.path)
        info = file_system.copy_file_or_directory(body.path, body.new_path)
        return FileCopyResponse(success=True, info=info)
    except Exception as e:
        LOGGER.error(f"Error copying file or directory: {e}")
        return FileCopyResponse(success=False, message=str(e))


@tool("files_move")
@requires("edit")
async def move_file_or_directory(
    *,
    request: Request,
    body: FileMoveRequest,
) -> FileMoveResponse:
    """Move a file or directory to a new path."""
    try:
        file_system.get_info(body.path)
        info = file_system.move_file_or_directory(body.path, body.new_path)
        return FileMoveResponse(success=True, info=info)
    except Exception as e:
        LOGGER.error(f"Error moving file or directory: {e}")
        return FileMoveResponse(success=False, message=str(e))


@tool("files_update")
@requires("edit")
async def update_file(
    *,
    request: Request,
    body: FileUpdateRequest,
) -> FileUpdateResponse:
    """Replace a file's contents and reload any notebook it backs."""
    app_state = AppState(request)
    try:
        file_system.get_info(body.path)
        info = file_system.update_file(body.path, body.contents)

        # Handle marimo notebook reload if there's an active session
        session_manager = app_state.session_manager
        await session_manager.trigger_file_change(body.path)

        return FileUpdateResponse(success=True, info=info)
    except Exception as e:
        LOGGER.error(f"Error updating file or directory: {e}")
        return FileUpdateResponse(success=False, message=str(e))


@tool("files_open")
@requires("edit")
async def open_file(
    *,
    request: Request,
    body: FileOpenRequest,
) -> BaseResponse:
    """Open a file in the user's editor."""
    try:
        file_system.get_info(body.path)
        success = file_system.open_in_editor(body.path, body.line_number)
        return SuccessResponse(success=success)
    except Exception as e:
        LOGGER.error(f"Error opening file: {e}")
        return ErrorResponse(success=False, message=str(e))


@tool("files_search")
@requires("edit")
async def search_files(
    *,
    request: Request,
    body: FileSearchRequest,
) -> FileSearchResponse:
    """Search for files and directories by name."""
    files = await asyncio.to_thread(
        file_system.search,
        query=body.query,
        path=body.path,
        include_directories=body.include_directories,
        include_files=body.include_files,
        include_hidden=body.include_hidden,
        depth=body.depth,
        limit=body.limit,
    )
    return FileSearchResponse(
        files=files, query=body.query, total_found=len(files)
    )
