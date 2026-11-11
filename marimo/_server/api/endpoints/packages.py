# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from starlette.authentication import requires

from marimo._config.settings import GLOBAL_SETTINGS
from marimo._environments import script_metadata
from marimo._environments.backends import backend_for
from marimo._environments.errors import (
    EnvironmentManagerError,
    SandboxRestartRequired,
)
from marimo._environments.sandbox import Backend, NotebookSandbox
from marimo._messaging.notification import (
    EnvironmentOperationNotification,
    PackageStatusType,
)
from marimo._runtime.packages.operations import environment_operation
from marimo._runtime.packages.package_manager import PackageManager
from marimo._runtime.packages.package_managers import create_package_manager
from marimo._runtime.packages.sandbox_package_manager import (
    SandboxPackageManager,
)
from marimo._runtime.packages.utils import split_packages
from marimo._server.api.deps import AppState
from marimo._server.api.tools import tool
from marimo._server.models.packages import (
    AddPackageRequest,
    DependencyTreeResponse,
    ListPackagesResponse,
    PackageInstallationContext,
    PackageManagerContext,
    PackageOperationResponse,
    RemovePackageRequest,
    SandboxPackageContext,
    SandboxRequest,
    SandboxResponse,
    SyncSandboxResponse,
    UpdateManifestRequest,
)
from marimo._utils.http import HTTPException

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request


@tool("packages_add", input=AddPackageRequest, output=PackageOperationResponse)
@requires("edit")
async def add_package(
    *, request: Request, body: AddPackageRequest
) -> PackageOperationResponse:
    """Install a package into the notebook's environment."""
    package_manager = _get_package_manager(request)
    if not package_manager.is_manager_installed():
        package_manager.alert_not_installed()
        return PackageOperationResponse.of_failure(
            f"{package_manager.name} is not available. "
            f"Check out the docs for installation instructions: {package_manager.docs_url}"
        )

    packages: PackageStatusType = {body.package: "running"}
    with environment_operation(
        "install", packages, "kernel", _operation_notifier(request)
    ) as operation:
        upgrade = body.upgrade or False
        group = body.group or None
        success = await package_manager.install(
            body.package,
            version=None,
            upgrade=upgrade,
            group=group,
            log_callback=lambda line: operation.update({body.package: line}),
        )

        # Update the script metadata
        filename = _get_filename(request)
        if (
            success
            and filename is not None
            and GLOBAL_SETTINGS.MANAGE_SCRIPT_METADATA
        ):
            await asyncio.to_thread(
                package_manager.update_notebook_script_metadata,
                filepath=filename,
                packages_to_add=split_packages(body.package),
                upgrade=upgrade,
            )

        packages[body.package] = (
            "succeeded"
            if success
            else "restart-required"
            if package_manager.restart_required
            else "failed"
        )

        if success:
            return PackageOperationResponse.of_success()

        if package_manager.restart_required:
            return PackageOperationResponse(
                success=False, restart_required=True
            )

        return PackageOperationResponse.of_failure(
            _failure_message(package_manager)
            or f"Failed to install {body.package}. See terminal for error logs."
        )


@tool(
    "packages_remove",
    input=RemovePackageRequest,
    output=PackageOperationResponse,
)
@requires("edit")
async def remove_package(
    *, request: Request, body: RemovePackageRequest
) -> PackageOperationResponse:
    """Uninstall a package from the notebook's environment."""
    # TODO: Use `uv remove` instead of package manager uninstall for better dependency management
    package_manager = _get_package_manager(request)
    if not package_manager.is_manager_installed():
        package_manager.alert_not_installed()
        return PackageOperationResponse.of_failure(
            f"{package_manager.name} is not available. "
            f"Check out the docs for installation instructions: {package_manager.docs_url}"
        )

    packages: PackageStatusType = {body.package: "running"}
    with environment_operation(
        "remove", packages, "kernel", _operation_notifier(request)
    ):
        group = body.group or None
        success = await package_manager.uninstall(body.package, group=group)

        # Update the script metadata
        filename = _get_filename(request)
        if (
            success
            and filename is not None
            and GLOBAL_SETTINGS.MANAGE_SCRIPT_METADATA
        ):
            await asyncio.to_thread(
                package_manager.update_notebook_script_metadata,
                filepath=filename,
                packages_to_remove=split_packages(body.package),
                upgrade=False,
            )

        packages[body.package] = (
            "succeeded"
            if success
            else "restart-required"
            if package_manager.restart_required
            else "failed"
        )

        if success:
            return PackageOperationResponse.of_success()

        if package_manager.restart_required:
            return PackageOperationResponse(
                success=False, restart_required=True
            )

        return PackageOperationResponse.of_failure(
            _failure_message(package_manager)
            or f"Failed to uninstall {body.package}. See terminal for error logs."
        )


def _operation_notifier(
    request: Request,
) -> Callable[[EnvironmentOperationNotification], None]:
    session = AppState(request).get_current_session()
    loop = asyncio.get_running_loop()

    def notify(notification: EnvironmentOperationNotification) -> None:
        if session is not None:
            # Package-manager output may arrive on a worker thread. Keep it
            # ordered with snapshots and other notifications on the session loop.
            loop.call_soon_threadsafe(session.notify, notification, None)

    return notify


def _failure_message(package_manager: PackageManager) -> str | None:
    if isinstance(package_manager, SandboxPackageManager):
        return package_manager.last_error
    return None


@tool("packages_list", output=ListPackagesResponse)
@requires("edit")
async def list_packages(*, request: Request) -> ListPackagesResponse:
    """List the packages installed in the notebook's environment."""
    package_manager = _get_package_manager(request)
    if not package_manager.is_manager_installed():
        package_manager.alert_not_installed()
        return ListPackagesResponse(packages=[])

    packages = await asyncio.to_thread(package_manager.list_packages)

    return ListPackagesResponse(packages=packages)


@tool("packages_tree", output=DependencyTreeResponse)
@requires("edit")
async def dependency_tree(*, request: Request) -> DependencyTreeResponse:
    """Describe the notebook environment's dependency tree."""
    package_manager = _get_package_manager(request)
    context = _get_package_installation_context(package_manager)

    filename = _get_filename(request)
    is_sandbox = (
        filename is not None and GLOBAL_SETTINGS.SANDBOX_MODE is not None
    )
    if is_sandbox:
        tree = await asyncio.to_thread(
            package_manager.dependency_tree, filename
        )
    else:
        tree = await asyncio.to_thread(package_manager.dependency_tree)
    return DependencyTreeResponse(tree=tree, context=context)


def _get_package_manager(request: Request) -> PackageManager:
    app_state = AppState(request)
    session = app_state.get_current_session()
    if not session:
        config = app_state.config_manager.get_config()
        return create_package_manager(config.package_management.manager)

    config_manager = app_state.app_config_manager

    # Check if IPC mode - use kernel's venv Python
    python_exe: str | None = None
    from marimo._session.managers.ipc import IPCKernelManagerImpl
    from marimo._session.session import SessionImpl

    if isinstance(session, SessionImpl):
        sandbox = session.notebook_sandbox
        if isinstance(sandbox, NotebookSandbox):
            return SandboxPackageManager(sandbox)

        kernel_manager = session._kernel_manager
        if isinstance(kernel_manager, IPCKernelManagerImpl):
            python_exe = kernel_manager.venv_python

    config = config_manager.get_config()
    return create_package_manager(
        config.package_management.manager,
        python_exe=python_exe,
    )


def _get_package_installation_context(
    package_manager: PackageManager,
) -> PackageInstallationContext:
    if isinstance(package_manager, SandboxPackageManager):
        return SandboxPackageContext(backend=package_manager.backend)
    return PackageManagerContext(name=package_manager.name)


def _get_filename(request: Request) -> str | None:
    session = AppState(request).get_current_session()
    if session is None:
        return None
    return session.app_file_manager.filename


def _runs_in_configured_venv(state: AppState, path: str) -> bool:
    """True when auto mode will start `path` in its `[tool.marimo.venv]`."""
    if not GLOBAL_SETTINGS.SANDBOX_AUTO:
        return False
    config = state.config_manager_at_file(path).get_config(hide_secrets=False)
    return bool(config.venv.path)


def _sandbox_source(
    request: Request, file_key: str | None, *, mutation: bool = False
) -> tuple[NotebookSandbox | None, str | None, Backend | None]:
    state = AppState(request)
    manager = state.session_manager
    key = file_key or manager.workspace.get_unique_file_key()
    if (
        mutation
        and key is not None
        and manager.is_session_starting(
            state.require_current_session_id(), key
        )
    ):
        raise HTTPException(409, "Wait for sandbox preparation to finish.")
    session = state.get_current_session()
    if session is not None:
        sandbox = getattr(session, "notebook_sandbox", None)
        if isinstance(sandbox, NotebookSandbox):
            return sandbox, sandbox.source, sandbox.backend
        return None, None, None
    if not manager.sandbox:
        return None, None, None
    path = manager.workspace.resolve(key) if key else None
    if path is not None and _runs_in_configured_venv(state, path):
        # NB. Same answer the session path gives once the kernel is up: a
        # configured-venv kernel has no NotebookSandbox.
        return None, None, None
    return None, path, backend_for(path)


@tool("packages_sandbox", input=SandboxRequest, output=SandboxResponse)
@requires("edit")
async def get_sandbox(
    *, request: Request, body: SandboxRequest
) -> SandboxResponse:
    """Describe the notebook's sandbox and its manifest."""
    _, path, backend = _sandbox_source(request, body.file_key)
    try:
        manifest = (
            await asyncio.to_thread(script_metadata.read_manifest, path)
            if path is not None
            else None
        )
    except (OSError, ValueError, EnvironmentManagerError) as error:
        raise HTTPException(400, str(error)) from error
    return SandboxResponse(backend=backend, manifest=manifest, filename=path)


@tool("packages_manifest", input=UpdateManifestRequest, output=SandboxResponse)
@requires("edit")
async def update_manifest(
    *, request: Request, body: UpdateManifestRequest
) -> SandboxResponse:
    """Replace the notebook's inline script manifest."""
    _, path, backend = _sandbox_source(request, body.file_key, mutation=True)
    if path is None or backend is None:
        raise HTTPException(400, "No notebook manifest is available to edit.")
    try:
        manifest = await asyncio.to_thread(
            script_metadata.write_manifest,
            path,
            body.contents,
            previous=body.previous,
        )
    except script_metadata.ManifestConflictError as error:
        raise HTTPException(409, str(error)) from error
    except (OSError, ValueError, EnvironmentManagerError) as error:
        raise HTTPException(400, str(error)) from error
    return SandboxResponse(backend=backend, manifest=manifest, filename=path)


@tool("packages_sync", input=SandboxRequest, output=SyncSandboxResponse)
@requires("edit")
async def sync_sandbox(
    *, request: Request, body: SandboxRequest
) -> SyncSandboxResponse:
    """Sync the notebook's sandbox with its manifest."""
    sandbox, _, backend = _sandbox_source(
        request, body.file_key, mutation=True
    )
    if backend is None:
        raise HTTPException(400, "This notebook does not use a sandbox.")
    if sandbox is None:
        # Connection creation owns provisioning and streams its progress.
        return SyncSandboxResponse(success=True, reconnect=True)
    try:
        with environment_operation(
            "sync", {}, "kernel", _operation_notifier(request)
        ) as operation:
            await sandbox.sync_async(
                on_output=lambda line: operation.update({"environment": line})
            )
    except SandboxRestartRequired as error:
        return SyncSandboxResponse(
            success=False, error=str(error), restart_required=True
        )
    except (EnvironmentManagerError, OSError) as error:
        return SyncSandboxResponse(success=False, error=str(error))
    return SyncSandboxResponse(success=True)
