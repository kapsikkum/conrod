r"""The application window.

Conrod runs a local server and shows it in a chromeless browser window --
Edge's "app mode", which on Windows 11 is the same WebView2 engine a native
webview would use, minus the toolbar.

It used to use pywebview, which reaches WebView2 through pythonnet. That
worked from source and inside one frozen build, then failed inside another
built from the same commit:

    Failed to resolve Python.Runtime.Loader.Initialize from
    ...\_internal\pythonnet\runtime\Python.Runtime.dll

The bundled payload was byte-identical to the one that worked, so the failure
is environmental and not something the build can guarantee away. A .NET
bridge is too much machinery to stake the main window on when the window is
just a page; launching a browser in app mode has no moving parts to break.

The folder picker that pywebview provided is now a real Win32 dialog served
over HTTP (see nativeui.py), so it works in every mode -- including the plain
browser fallback, which never had one.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import server
from .config import DATA_ROOT, Settings
from .mapping import NumberMap

WINDOW = (1440, 940)


def _free_port(preferred: int = 8760) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def _wait_until_up(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def _browser_binaries() -> list[Path]:
    """Chromium browsers that support --app, most preferred first."""
    roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
             os.environ.get("LOCALAPPDATA")]
    relative = [
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
        Path("BraveSoftware/Brave-Browser/Application/brave.exe"),
    ]
    found = []
    for rel in relative:
        for root in roots:
            if not root:
                continue
            candidate = Path(root) / rel
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def _open_app_window(url: str) -> subprocess.Popen | None:
    """Open a chromeless window and return the process owning it.

    A dedicated --user-data-dir matters for two reasons: without it the
    command hands the URL to an already-running browser and returns
    immediately, leaving nothing to wait on, and the window would inherit the
    user's extensions and session.
    """
    profile = DATA_ROOT / "window"
    profile.mkdir(parents=True, exist_ok=True)

    width, height = WINDOW
    for binary in _browser_binaries():
        try:
            return subprocess.Popen(
                [str(binary), f"--app={url}",
                 f"--user-data-dir={profile}",
                 f"--window-size={width},{height}",
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-features=Translate,MediaRouter"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            continue
    return None


def launch(settings: Settings | None = None, number_map: NumberMap | None = None,
           *, force_browser: bool = False, serve_only: bool = False,
           port: int | None = None) -> int:
    settings = settings or Settings.load()
    server.configure(settings, number_map or NumberMap())

    port = port or _free_port()
    url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(server.app, host="127.0.0.1", port=port,
                            log_level="warning")
    api_server = uvicorn.Server(config)
    thread = threading.Thread(target=api_server.run, daemon=True)
    thread.start()

    if not _wait_until_up(port):
        print("The application server did not start.", file=sys.stderr)
        return 1

    if serve_only:
        print(f"Conrod is serving on {url}")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return 0

    window = None if force_browser else _open_app_window(url)
    if window is not None:
        try:
            window.wait()          # the app lives as long as its window
        except KeyboardInterrupt:
            window.terminate()
        return 0

    if not force_browser:
        print("No Chromium browser found for an app window; using the default "
              "browser instead.", file=sys.stderr)
    print(f"Conrod is running at {url}")
    webbrowser.open(url)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0
