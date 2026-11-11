# Copyright 2026 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
import subprocess
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import msgspec
import pytest
from starlette.requests import Request

from marimo._server.api.utils import (
    get_code_mode_credentials,
    install_packages_on_server,
    open_url_in_browser,
)

if TYPE_CHECKING:
    from starlette.types import Scope

    from marimo._server.api.deps import AppStateBase


class _SampleForm(msgspec.Struct):
    name: str
    count: int


@pytest.mark.parametrize(
    ("platform", "implementation", "start_new_session"),
    [
        ("linux", "cpython", True),
        ("darwin", "cpython", True),
        ("win32", "cpython", False),
        ("cygwin", "cpython", False),
        ("linux", "graalpy", False),
    ],
)
def test_open_browser_detaches_without_preexec_callback(
    platform: str, implementation: str, start_new_session: bool
) -> None:
    with (
        patch("marimo._server.api.utils.which", return_value="/bin/xdg-open"),
        patch(
            "marimo._server.api.utils.sys",
            SimpleNamespace(
                platform=platform,
                implementation=SimpleNamespace(name=implementation),
            ),
        ),
        patch("marimo._server.api.utils.subprocess.Popen") as popen,
    ):
        open_url_in_browser("default", "http://localhost:2718")

    popen.assert_called_once_with(
        ["xdg-open", "http://localhost:2718"],
        start_new_session=start_new_session,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


async def test_install_packages_on_server_uses_uv_when_available() -> None:
    with (
        patch(
            "marimo._runtime.packages.pypi_package_manager.UvPackageManager.is_manager_installed",
            return_value=True,
        ),
        patch(
            "marimo._runtime.packages.pypi_package_manager.UvPackageManager.install",
            new=AsyncMock(return_value=True),
        ) as install,
    ):
        await install_packages_on_server({"nbformat": ""})

    install.assert_awaited_once_with("nbformat", version=None)


async def test_install_packages_on_server_reports_install_failure() -> None:
    with (
        patch(
            "marimo._runtime.packages.pypi_package_manager.UvPackageManager.is_manager_installed",
            return_value=False,
        ),
        patch(
            "marimo._runtime.packages.pypi_package_manager.PipPackageManager.is_manager_installed",
            return_value=True,
        ),
        patch(
            "marimo._runtime.packages.pypi_package_manager.PipPackageManager.install",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(
            RuntimeError,
            match="Failed to install nbformat into the server Python",
        ),
    ):
        await install_packages_on_server({"nbformat": ""})


async def test_install_packages_on_server_checks_pip_off_event_loop() -> None:
    check_started = asyncio.Event()
    release_check = threading.Event()
    loop = asyncio.get_running_loop()

    def check_pip() -> bool:
        loop.call_soon_threadsafe(check_started.set)
        release_check.wait(timeout=1)
        return True

    with (
        patch(
            "marimo._runtime.packages.pypi_package_manager.UvPackageManager.is_manager_installed",
            return_value=False,
        ),
        patch(
            "marimo._runtime.packages.pypi_package_manager.PipPackageManager.is_manager_installed",
            side_effect=check_pip,
        ),
        patch(
            "marimo._runtime.packages.pypi_package_manager.PipPackageManager.install",
            new=AsyncMock(return_value=True),
        ),
    ):
        install = asyncio.create_task(
            install_packages_on_server({"nbformat": ""})
        )
        await check_started.wait()
        assert not install.done()

        release_check.set()
        await install


def _fake_app_state(
    *,
    host: str = "localhost",
    port: int = 2718,
    base_url: str = "",
    auth_token: str = "tok",
) -> AppStateBase:
    """Minimal duck-typed stand-in for AppStateBase exposing only the
    attributes that get_code_mode_credentials reads."""
    state = SimpleNamespace(
        host=host,
        port=port,
        base_url=base_url,
        session_manager=SimpleNamespace(auth_token=auth_token),
    )
    return cast("AppStateBase", cast(object, state))


def _fake_request(
    *,
    scheme: str = "http",
    host_header: str = "evil.example.com:80",
) -> Request:
    """Build a Starlette Request with a chosen scheme and Host header
    without spinning up an app."""
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "scheme": scheme,
        "server": ("localhost", 2718),
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"host", host_header.encode())],
    }
    return Request(scope)


def test_get_code_mode_credentials_uses_configured_host_not_request_host_header() -> (
    None
):
    """Regression guard for the security property documented on
    get_code_mode_credentials: the server URL must come from the server's
    configured host/port, not from the (spoofable) Host header.
    Loopback addresses are pretty-printed to `localhost`."""
    url, token = get_code_mode_credentials(
        _fake_app_state(host="127.0.0.1", port=2718, auth_token="secret"),
        _fake_request(host_header="evil.example.com:80"),
    )
    assert url == "http://localhost:2718"
    assert token == "secret"


def test_get_code_mode_credentials_strips_trailing_slash_from_base_url() -> (
    None
):
    url, _ = get_code_mode_credentials(
        _fake_app_state(base_url="/notebook/"),
        _fake_request(),
    )
    assert url == "http://localhost:2718/notebook"


def test_get_code_mode_credentials_includes_non_empty_base_url() -> None:
    url, _ = get_code_mode_credentials(
        _fake_app_state(base_url="/notebook"),
        _fake_request(),
    )
    assert url == "http://localhost:2718/notebook"


def test_get_code_mode_credentials_empty_base_url() -> None:
    url, _ = get_code_mode_credentials(
        _fake_app_state(base_url=""),
        _fake_request(),
    )
    assert url == "http://localhost:2718"


def test_get_code_mode_credentials_propagates_https_scheme() -> None:
    url, _ = get_code_mode_credentials(
        _fake_app_state(),
        _fake_request(scheme="https"),
    )
    assert url == "https://localhost:2718"


@pytest.mark.parametrize(
    ("host", "expected_url"),
    [
        # Loopback addresses get pretty-printed to "localhost".
        ("127.0.0.1", "http://localhost:2718"),
        ("::1", "http://localhost:2718"),
        # Bracket-wrapped loopback (user passed [::1]) — strip + map.
        ("[::1]", "http://localhost:2718"),
        # Bind-all sentinels are NOT routable for an internal callback;
        # they must be mapped to localhost so Playwright can connect.
        ("0.0.0.0", "http://localhost:2718"),
        ("::", "http://localhost:2718"),
        # Real IPv6 literals must be bracketed per RFC 3986.
        ("fd00::cafe", "http://[fd00::cafe]:2718"),
        ("2001:db8::1", "http://[2001:db8::1]:2718"),
        # User-supplied brackets on a real address — strip and re-add.
        ("[fd00::cafe]", "http://[fd00::cafe]:2718"),
        # Zone IDs are interface-specific and not valid in URLs.
        ("fe80::1%eth0", "http://[fe80::1]:2718"),
        ("[fe80::1%lo0]", "http://[fe80::1]:2718"),
        # Regular hostnames pass through unchanged.
        ("example.com", "http://example.com:2718"),
    ],
)
def test_get_code_mode_credentials_normalizes_host(
    host: str, expected_url: str
) -> None:
    """The server URL must be a routable, URL-safe destination — bind-all
    addresses mapped to loopback, IPv6 bracketed, zone IDs stripped —
    so code-mode callbacks (e.g. Playwright screenshots) actually reach
    the server."""
    url, _ = get_code_mode_credentials(
        _fake_app_state(host=host),
        _fake_request(),
    )
    assert url == expected_url
