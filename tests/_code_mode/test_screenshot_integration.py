# Copyright 2026 Marimo. All rights reserved.
"""Run with MARIMO_TEST_SCREENSHOTS=1 after building the frontend.

The Playwright CI job supplies built assets and matching Chromium binaries.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import socket
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
import pytest

from tests.mcp_request import (
    request_headers,
    stream_headers,
    stream_payload,
    tool_payload,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from playwright.async_api import Page


async def _call_sessions(
    client: httpx.AsyncClient, url: str
) -> httpx.Response:
    """Call the editor's `sessions` tool at `url`."""
    return await client.post(
        url,
        json=tool_payload("sessions"),
        headers=request_headers("tools/call", "sessions"),
    )


async def _sessions(client: httpx.AsyncClient) -> dict[str, Any]:
    """The server's sessions, by session id, from its `sessions` tool."""
    response = await _call_sessions(client, "/mcp")
    response.raise_for_status()
    sessions: dict[str, Any] = response.json()["result"]["structuredContent"]
    return sessions


async def _running(lines: AsyncIterator[str], seen: set[str]) -> str:
    """The next notebook whose runtime runs, from a `catalog` stream."""
    async for line in lines:
        if not line.startswith("data:"):
            continue
        message = json.loads(line.removeprefix("data:"))
        event = message.get("params", {}).get("data", {})
        runtime = event.get("data", {}).get("runtime", {})
        notebook_id = runtime.get("notebook_id")
        if runtime.get("status") == "running" and notebook_id not in seen:
            seen.add(notebook_id)
            return str(notebook_id)
    raise AssertionError("The catalog ended")


async def _execute(
    client: httpx.AsyncClient,
    notebook_id: str,
    code: str,
    *,
    verify_png: bool = False,
) -> str:
    response = await client.post(
        "/mcp",
        json=tool_payload(
            "kernel_execute", {"notebook_id": notebook_id, "code": code}
        ),
        headers=request_headers("tools/call", "kernel_execute"),
        timeout=90,
    )
    response.raise_for_status()
    # A long call answers as a stream: pings, then the result.
    body = next(
        (
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ),
        response.text,
    )
    result = json.loads(body)["result"]["structuredContent"]
    stdout = "".join(result["stdout"])
    assert result["success"], "".join(result["stderr"])
    if verify_png:
        assert result["output_mimetype"] == "image/png"
        image_url = result["output"]
        assert image_url.startswith("data:image/png;base64,")
        png = base64.b64decode(image_url.split(",", 1)[1], validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert "PNG_SHA=" + hashlib.sha256(png).hexdigest() in stdout
    return stdout.strip()


@pytest.mark.integration
@pytest.mark.timeout(180)
@pytest.mark.skipif(
    os.environ.get("MARIMO_TEST_SCREENSHOTS") != "1",
    reason="Requires built frontend assets and Playwright Chromium",
)
async def test_screenshots_attach_to_live_notebook(tmp_path: Path) -> None:
    from playwright.async_api import async_playwright

    token = "screenshot-test+&=#?"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token)
    token_file.chmod(0o600)
    files = [tmp_path / "Notebook A + & # ?.py", tmp_path / "Notebook B.py"]
    for file in files:
        file.write_text(
            "import marimo\napp = marimo.App()\n"
            "@app.cell\ndef _():\n    value = 1\n    return (value,)\n"
            "if __name__ == '__main__':\n    app.run()\n"
        )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}/capture-check"
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        server = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "marimo",
            "edit",
            str(tmp_path),
            "--headless",
            "--no-sandbox",
            "--skip-update-check",
            "--port",
            str(port),
            "--base-url",
            "/capture-check",
            "--token-password-file",
            str(token_file),
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with httpx.AsyncClient(
                base_url=base + "/",
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        response = await _call_sessions(client, "/mcp")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert server.returncode is None, log_path.read_text()
                    assert time.monotonic() < deadline, log_path.read_text()
                    await asyncio.sleep(0.1)
                async with httpx.AsyncClient() as anonymous:
                    assert (
                        await _call_sessions(anonymous, base + "/mcp")
                    ).status_code != 200

                async with (
                    async_playwright() as playwright,
                    client.stream(
                        "POST",
                        "/mcp",
                        json=stream_payload("catalog", {"types": ["runtime"]}),
                        headers=stream_headers("catalog"),
                    ) as catalog,
                ):
                    browser = await playwright.chromium.launch()
                    keys = [str(file) for file in files] + [
                        "__new__capture-test"
                    ]
                    lines = catalog.aiter_lines()
                    seen: set[str] = set()
                    editors: list[tuple[str, Page, str]] = []
                    for key in keys:
                        context = await browser.new_context()
                        page = await context.new_page()
                        await page.goto(
                            base
                            + "/?"
                            + urlencode({"file": key, "access_token": token})
                        )
                        # A page's session runs its notebook once it joined.
                        editors.append(
                            (key, page, await _running(lines, seen))
                        )
                    sessions_before = await _sessions(client)
                    assert len(sessions_before) == 3
                    for index, (key, page, notebook_id) in enumerate(editors):
                        marker = f"LIVE NOTEBOOK {index}"
                        source = f"import marimo as mo\nmo.Html('<div>{marker}</div>')"
                        if index == 2:
                            operation = (
                                f"cell_id = ctx.create_cell({source!r})"
                            )
                        else:
                            operation = (
                                "cell_id = ctx.cells[-1].id\n"
                                "    previous_code = ctx.cells[cell_id].code\n"
                                f"    ctx.edit_cell(cell_id, code={source!r})"
                            )
                        # Keep the same context: its empty snapshot must not
                        # prevent capturing output rendered after run_cell.
                        code = f"""
import marimo._code_mode as cm
from urllib.parse import parse_qs, urlsplit
ctx = cm.get_context()
assert all(cell.output is None or cell.output.data == "" for cell in ctx.cells)
async with ctx:
    {operation}
    ctx.run_cell(cell_id)
image = await ctx.screenshot(cell_id)
session = ctx._screenshot_session
assert image.startswith(b"\\x89PNG\\r\\n\\x1a\\n")
assert {marker!r} in await session._page.locator("#output-" + cell_id).inner_text()
assert parse_qs(urlsplit(session._page.url).query)["file"] == [{key!r}]
assert parse_qs(urlsplit(session._page.url).query)["kiosk"] == ["true"]
browser = session._browser
page = session._page
await ctx.close_screenshot_session()
assert not browser.is_connected()
assert page.is_closed()

# A bound, non-listening port makes real Chromium navigation fail.
import socket
from playwright.async_api import Error
with socket.socket() as unavailable:
    unavailable.bind(("127.0.0.1", 0))
    session._server_url = "http://127.0.0.1:" + str(unavailable.getsockname()[1])
    try:
        await session.capture(cell_id)
    except Error as error:
        assert "net::ERR_CONNECTION_" in str(error)
    else:
        raise AssertionError("Navigation should fail")
assert session._page is None
assert session._browser is None
assert session._playwright is None
session._server_url = {base!r}
retry_image = await session.capture(cell_id)
assert retry_image.startswith(b"\\x89PNG\\r\\n\\x1a\\n")
assert {marker!r} in await session._page.locator("#output-" + cell_id).inner_text()
await session.close()
import hashlib
print("PNG_SHA=" + hashlib.sha256(image).hexdigest())
print("CELL_ID=" + cell_id)
image
"""
                        stdout = await _execute(
                            client, notebook_id, code, verify_png=True
                        )
                        cell_id = next(
                            line.removeprefix("CELL_ID=")
                            for line in stdout.splitlines()
                            if line.startswith("CELL_ID=")
                        )
                        await page.get_by_text(marker, exact=True).wait_for()
                        assert (
                            await _execute(
                                client, notebook_id, "print('still connected')"
                            )
                            == "still connected"
                        )
                        assert (
                            await page.locator("#output-" + cell_id).count()
                            == 1
                        )
                    assert set(sessions_before) == set(await _sessions(client))
                    await browser.close()
        finally:
            if server.returncode is None:
                os.killpg(server.pid, signal.SIGINT)
                try:
                    await asyncio.wait_for(server.wait(), timeout=15)
                except TimeoutError:
                    os.killpg(server.pid, signal.SIGKILL)
                    await asyncio.wait_for(server.wait(), timeout=5)
