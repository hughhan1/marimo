# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from starlette.authentication import requires
from starlette.exceptions import HTTPException

from marimo import _loggers
from marimo._convert.common.filename import get_download_filename
from marimo._convert.script import UnsupportedAsyncCodeError
from marimo._export.dependencies import (
    get_missing_export_packages,
    get_missing_export_setup,
    install_export_setup,
)
from marimo._export.exporter import (
    AutoExporter,
    Exporter,
    export_markdown,
    export_script,
    render_pdf,
)
from marimo._export.requests import (
    HTMLExportRequest,
    IPYNBExportRequest,
    MarkdownExportRequest,
    PDFExportRequest,
    ScriptExportRequest,
)
from marimo._export.serialization import serialize_notebook_snapshot
from marimo._runtime.commands import InstallPackagesCommand
from marimo._schemas.export import (
    AutoExportAsIPYNBRequest,
    AutoExportAsMarkdownRequest,
    ExportAsHTMLRequest,
    ExportAsIPYNBRequest,
    ExportAsMarkdownRequest,
    ExportAsPDFRequest,
    ExportAsScriptRequest,
    ExportAvailabilityResponse,
    ExportFormatAvailability,
    InstallExportRequirementsRequest,
    UpdateCellOutputsRequest,
    to_html_export_layout,
    to_html_export_options,
    to_ipynb_export_options,
    to_markdown_export_options,
    to_pdf_export_options,
)
from marimo._schemas.export_options import (
    SERVER_EXPORT_FORMATS,
    IPYNBExportOptions,
    MarkdownExportOptions,
    ServerExportFormat,
)
from marimo._server.api.deps import AppState
from marimo._server.api.tools import FileResult, after_reply, tool
from marimo._server.api.utils import (
    enforce_consumer_capability,
    install_packages_on_server,
    notify_server_missing_packages,
)
from marimo._server.models.models import SuccessResponse
from marimo._utils.http import HTTPStatus

if TYPE_CHECKING:
    from starlette.requests import Request

LOGGER = _loggers.marimo_logger()


auto_exporter = AutoExporter()


async def _get_export_format_availability(
    export_format: ServerExportFormat,
) -> ExportFormatAvailability:
    missing_packages = get_missing_export_packages(export_format)
    missing_setup = (
        []
        if missing_packages
        else await get_missing_export_setup(export_format)
    )
    return ExportFormatAvailability(
        format=export_format,
        dependencies_available=not missing_packages and not missing_setup,
        missing_packages=missing_packages,
        missing_setup=missing_setup,
    )


async def _get_export_availability() -> ExportAvailabilityResponse:
    return ExportAvailabilityResponse(
        source="server",
        formats=[
            await _get_export_format_availability(export_format)
            for export_format in SERVER_EXPORT_FORMATS
        ],
    )


@tool("export_availability", output=ExportAvailabilityResponse)
@requires("read")
async def get_export_availability(
    *,
    request: Request,
) -> ExportAvailabilityResponse:
    """Report which export formats the server can produce."""
    del request
    return await _get_export_availability()


@tool(
    "export_requirements_install",
    input=InstallExportRequirementsRequest,
    output=ExportAvailabilityResponse,
)
@requires("edit")
async def install_export_requirements(
    *,
    request: Request,
    body: InstallExportRequirementsRequest,
) -> ExportAvailabilityResponse:
    """Install what an export format needs on the server."""
    app_state = AppState(request)
    config = app_state.app_config_manager.get_config()
    command = InstallPackagesCommand(
        manager=config.package_management.manager,
        versions={},
        source="server",
    )
    enforce_consumer_capability(app_state, command)

    format_availability = await _get_export_format_availability(body.format)
    if format_availability.missing_packages:
        await install_packages_on_server(
            dict.fromkeys(format_availability.missing_packages, "")
        )
        format_availability = await _get_export_format_availability(
            body.format
        )

    # Setup can only be probed after its Python packages are importable.
    for requirement in format_availability.missing_setup:
        await install_export_setup(requirement.name)

    availability = await _get_export_availability()
    target = next(
        item for item in availability.formats if item.format == body.format
    )
    if not target.dependencies_available:
        raise HTTPException(
            status_code=HTTPStatus.SERVER_ERROR,
            detail=(
                f"Failed to install requirements for "
                f"{body.format.upper()} export. Check the server logs."
            ),
        )
    return availability


@tool("export_html", input=ExportAsHTMLRequest, output=FileResult)
@requires("read")
async def export_as_html(
    *,
    request: Request,
    body: ExportAsHTMLRequest,
) -> FileResult:
    """Export the notebook, with its outputs, as an HTML file."""
    app_state = AppState(request)
    session = app_state.require_current_session()

    # Check if the file is named
    if not session.app_file_manager.is_notebook_named:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must have a name before exporting",
        )

    # Only include the code if allowed (edit mode or include_code=True)
    if not app_state.session_manager.should_send_code_to_frontend():
        body.include_code = False

    resolved_config = session.config_manager.get_config()
    app = session.app_file_manager.app
    layout = to_html_export_layout(
        body,
        fallback=session.app_file_manager.read_layout_config,
    )
    html, filename = Exporter().export_as_html(
        HTMLExportRequest(
            filename=session.app_file_manager.filename,
            app_code=app.to_py(),
            app_config=app.config,
            snapshot=serialize_notebook_snapshot(
                app,
                session.session_view,
                drop_virtual_file_outputs=False,
                include_model_notifications=True,
            ),
            display_config=resolved_config.display,
            options=to_html_export_options(body),
            layout=layout,
            sharing_config=resolved_config.sharing,
        )
    )

    return FileResult(
        data=html.encode(), media_type="text/html", filename=filename
    )


@tool(
    "export_auto_export_html",
    input=ExportAsHTMLRequest,
    output=SuccessResponse,
)
@requires("edit")
async def auto_export_as_html(
    *,
    request: Request,
    body: ExportAsHTMLRequest,
) -> SuccessResponse:
    """Snapshot the notebook as HTML beside it, unless already up to date."""
    app_state = AppState(request)
    session = app_state.require_current_session()

    session_view = session.session_view
    layout = to_html_export_layout(
        body,
        fallback=session.app_file_manager.read_layout_config,
    )

    if not session.app_file_manager.is_notebook_named:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must have a name before exporting",
        )

    # If we have already exported to HTML, don't do it again
    if not session_view.needs_auto_export_html(layout):
        LOGGER.debug("Already auto-exported to HTML")
        return SuccessResponse()

    # If session_view is empty (no outputs), don't export
    if session_view.is_empty():
        LOGGER.info("No outputs to export")
        return SuccessResponse()

    filename = session.app_file_manager.filename
    generation = session_view.auto_export_generation
    revision = auto_exporter.reserve_revision(filename, "html")

    async def _background_export() -> None:
        app = session.app_file_manager.app
        html, _filename = Exporter().export_as_html(
            HTMLExportRequest(
                filename=filename,
                app_code=app.to_py(),
                app_config=app.config,
                snapshot=serialize_notebook_snapshot(
                    app,
                    session_view,
                    drop_virtual_file_outputs=False,
                    include_model_notifications=True,
                ),
                display_config=session.config_manager.get_config().display,
                options=to_html_export_options(body),
                layout=layout,
            )
        )

        # Save the HTML file to disk, at `.marimo/<filename>.html`
        if session.app_file_manager.filename != filename:
            return
        committed = await auto_exporter.save_html(
            filename=filename,
            html=html,
            revision=revision,
        )
        if committed:
            session_view.mark_auto_export_html(layout, generation=generation)

    after_reply(request, _background_export)
    return SuccessResponse()


@tool("export_script", input=ExportAsScriptRequest, output=FileResult)
@requires("edit")
async def export_as_script(
    *,
    request: Request,
    body: ExportAsScriptRequest,
) -> FileResult:
    """Export the notebook as a flat Python script."""
    del body
    app_state = AppState(request)
    session = app_state.require_current_session()

    try:
        result = export_script(
            ScriptExportRequest(
                notebook=replace(
                    session.app_file_manager.app.to_ir(),
                    filename=session.app_file_manager.filename,
                ),
            )
        )
    except UnsupportedAsyncCodeError as error:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=str(error),
        ) from error

    return FileResult(
        data=result.text.encode(),
        media_type="text/plain",
        filename=result.download_filename,
    )


@tool("export_markdown", input=ExportAsMarkdownRequest, output=FileResult)
@requires("edit")
async def export_as_markdown(
    *,
    request: Request,
    body: ExportAsMarkdownRequest,
) -> FileResult:
    """Export the notebook as a Markdown file."""
    app_state = AppState(request)
    app_file_manager = app_state.require_current_session().app_file_manager

    if not app_file_manager.path:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must be saved before downloading",
        )

    result = export_markdown(
        MarkdownExportRequest(
            notebook=app_file_manager.app.to_ir(),
            options=to_markdown_export_options(
                body,
                filename=app_file_manager.filename,
                source_filename=app_file_manager.filename,
            ),
        )
    )

    return FileResult(
        data=result.text.encode(),
        media_type="text/plain",
        filename=result.download_filename,
    )


@tool("export_ipynb", input=ExportAsIPYNBRequest, output=FileResult)
@requires("edit")
async def export_as_ipynb(
    *,
    request: Request,
    body: ExportAsIPYNBRequest,
) -> FileResult:
    """Export the notebook as a Jupyter notebook."""
    app_state = AppState(request)
    session = app_state.require_current_session()

    if not session.app_file_manager.is_notebook_named:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must have a name before exporting",
        )

    ipynb = Exporter().export_as_ipynb(
        IPYNBExportRequest(
            app=session.app_file_manager.app,
            options=to_ipynb_export_options(body),
            session_view=session.session_view
            if body.include_outputs
            else None,
        )
    )

    filename = get_download_filename(
        session.app_file_manager.filename, "ipynb"
    )

    return FileResult(
        data=ipynb.encode(), media_type="text/plain", filename=filename
    )


@tool(
    "export_auto_export_markdown",
    input=AutoExportAsMarkdownRequest,
    output=SuccessResponse,
)
@requires("edit")
async def auto_export_as_markdown(
    *,
    request: Request,
    body: AutoExportAsMarkdownRequest,
) -> SuccessResponse:
    """Snapshot the notebook as Markdown beside it, unless already up to date."""
    del body
    app_state = AppState(request)
    session = app_state.require_current_session()
    session_view = session.session_view

    if not session.app_file_manager.is_notebook_named:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must have a name before exporting",
        )

    # If we have already exported to Markdown, don't do it again
    if not session_view.needs_export("md"):
        LOGGER.debug("Already auto-exported to Markdown")
        return SuccessResponse()

    filename = session.app_file_manager.filename
    generation = session_view.auto_export_generation
    revision = auto_exporter.reserve_revision(filename, "md")

    async def _background_export() -> None:
        # Reload the file manager to get the latest state
        session.app_file_manager.reload()

        result = export_markdown(
            MarkdownExportRequest(
                notebook=session.app_file_manager.app.to_ir(),
                options=MarkdownExportOptions(),
            )
        )

        # Save the Markdown file to disk, at `.marimo/<filename>.md`
        if session.app_file_manager.filename != filename:
            return
        committed = await auto_exporter.save_md(
            filename=filename,
            markdown=result.text,
            revision=revision,
        )
        if committed:
            session_view.mark_auto_export_md(generation=generation)

    after_reply(request, _background_export)
    return SuccessResponse()


@tool(
    "export_auto_export_ipynb",
    input=AutoExportAsIPYNBRequest,
    output=SuccessResponse,
)
@requires("edit")
async def auto_export_as_ipynb(
    *,
    request: Request,
    body: AutoExportAsIPYNBRequest,
) -> SuccessResponse:
    """Snapshot the notebook as a Jupyter notebook beside it, unless up to date."""
    del body
    app_state = AppState(request)
    session = app_state.require_current_session()
    session_view = session.session_view

    if not session.app_file_manager.is_notebook_named:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must have a name before exporting",
        )

    # If we have already exported to IPYNB, don't do it again
    if not session_view.needs_export("ipynb"):
        LOGGER.debug("Already auto-exported to IPYNB")
        return SuccessResponse()

    # Check server dependencies before scheduling the background task.
    missing_packages = get_missing_export_packages("ipynb")
    if missing_packages:
        LOGGER.warning(
            "Cannot snapshot to IPYNB: %s not installed",
            ", ".join(missing_packages),
        )
        unnotified_packages = [
            package
            for package in missing_packages
            if package not in session_view.notified_server_packages
        ]
        if unnotified_packages:
            notify_server_missing_packages(
                session,
                app_state.get_current_session_id(),
                unnotified_packages,
            )
            session_view.notified_server_packages.update(unnotified_packages)
        session_view.mark_auto_export_ipynb()
        return SuccessResponse()

    filename = session.app_file_manager.filename
    generation = session_view.auto_export_generation
    revision = auto_exporter.reserve_revision(filename, "ipynb")

    async def _background_export() -> None:
        # Reload the file manager to get the latest state
        session.app_file_manager.reload()

        ipynb = Exporter().export_as_ipynb(
            IPYNBExportRequest(
                app=session.app_file_manager.app,
                options=IPYNBExportOptions(sort_mode="top-down"),
                session_view=session_view,
            )
        )

        # Save the IPYNB file to disk, at `.marimo/<filename>.ipynb`
        if session.app_file_manager.filename != filename:
            return
        committed = await auto_exporter.save_ipynb(
            filename=filename,
            ipynb=ipynb,
            revision=revision,
        )
        if committed:
            session_view.mark_auto_export_ipynb(generation=generation)

    after_reply(request, _background_export)
    return SuccessResponse()


@tool("export_pdf", input=ExportAsPDFRequest, output=FileResult)
@requires("edit")
async def export_as_pdf(
    *, request: Request, body: ExportAsPDFRequest
) -> FileResult:
    """Export the notebook as a PDF."""
    app_state = AppState(request)
    session = app_state.require_current_session()

    if not session.app_file_manager.is_notebook_named:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="File must have a name before exporting",
        )

    export_request = PDFExportRequest(
        app=session.app_file_manager.app,
        session_view=session.session_view if body.include_outputs else None,
        options=to_pdf_export_options(body),
    )
    pdf_data = await render_pdf(export_request)
    if pdf_data is None:
        raise HTTPException(
            status_code=HTTPStatus.SERVER_ERROR, detail="Failed to export PDF"
        )
    filename = get_download_filename(session.app_file_manager.filename, "pdf")

    return FileResult(
        data=pdf_data, media_type="application/pdf", filename=filename
    )


@tool(
    "export_update_cell_outputs",
    input=UpdateCellOutputsRequest,
    output=SuccessResponse,
)
@requires("edit")
async def update_cell_outputs(
    *, request: Request, body: UpdateCellOutputsRequest
) -> SuccessResponse:
    """Record cells' rendered outputs for the next export."""
    app_state = AppState(request)
    session = app_state.require_current_session()
    session.session_view.update_cell_outputs(body.cell_ids_to_output)

    return SuccessResponse()
