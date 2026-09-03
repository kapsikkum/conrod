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

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import server, tray
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


# A window that closes this soon after opening did not get closed by anyone.
# Chromium hands the URL to whichever process already holds the profile and
# exits, so the launcher we were waiting on returns almost immediately.
HANDOFF_SECONDS = 5.0


def _close_orphan_windows() -> bool:
    """Close a browser left holding our profile after Conrod died.

    Kill Conrod any way other than closing its window -- Task Manager, a
    crash, a lost session -- and the browser it started stays open. The
    profile directory is then in use, so the next launch hands its URL to
    that stale window and exits within the second, and Conrod appears to do
    nothing at all when opened. Nothing but Conrod ever uses this profile, so
    a browser holding it while no Conrod is running is by definition ours to
    close.

    Returns whether anything was actually closed, because the only reason to
    ask is to decide whether reopening is worth a try.
    """
    if sys.platform != "win32":
        return False
    profile = str(DATA_ROOT / "window")
    script = (
        "$p = [regex]::Escape($env:CONROD_PROFILE); "
        # Narrow on the executable as well as the profile path. Matching the
        # path alone catches anything that merely mentions it -- a shell, an
        # editor, the search itself -- and this stops processes.
        "$browsers = 'msedge.exe','chrome.exe','brave.exe','vivaldi.exe',"
        "'opera.exe','thorium.exe'; "
        "$found = Get-CimInstance Win32_Process | Where-Object { "
        "  $_.ProcessId -ne $PID -and $browsers -contains $_.Name -and "
        "  $_.CommandLine -and $_.CommandLine -match $p }; "
        "$found | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "  -ErrorAction SilentlyContinue }; "
        "if ($found) { 'closed' }"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            env={**os.environ, "CONROD_PROFILE": profile},
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    if "closed" not in done.stdout:
        return False
    # The processes are gone but the profile's lock is released a moment
    # later, and reopening into a half-released profile hands off again.
    time.sleep(1.5)
    return True


class AppWindow:
    """The browser window, which can now be closed and opened again.

    The process used to live exactly as long as this: `wait()` returned and
    main() fell off the end. That is the wrong lifetime for a program whose
    main job takes hours, so the window is now something Conrod has rather
    than something Conrod is, and it has to survive being reopened.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._proc: subprocess.Popen | None = None
        self._closing = False
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def open(self) -> bool:
        with self._lock:
            if self.is_open():
                return True
            self._closing = False
            self._proc = _open_app_window(self.url)
            return self._proc is not None

    def wait(self) -> None:
        """Block until the window is gone."""
        for attempt in range(2):
            proc = self._proc
            if proc is None:
                return
            opened = time.monotonic()
            try:
                proc.wait()
            except KeyboardInterrupt:
                self.terminate()
                return
            if self._closing:
                return          # we closed it; nothing to investigate
            if time.monotonic() - opened >= HANDOFF_SECONDS or attempt:
                return
            # Gone in under five seconds: not closed, handed off. Clear the
            # stale window out of the way and open ours properly, once.
            if not _close_orphan_windows() or not self.open():
                return

    def terminate(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._closing = True
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


def _run_windowed(url: str, settings: Settings) -> bool:
    """Show the window, and keep Conrod alive in the tray once it is closed.

    False means no window could be opened at all and the caller should fall
    back to the default browser.
    """
    window = AppWindow(url)
    if not window.open():
        return False

    finished = threading.Event()

    def shutdown() -> None:
        finished.set()
        window.terminate()

    # Installing an update means replacing the folder this executable runs
    # from, which Windows refuses while it is open. So the updater's quit has
    # to end the process -- tucking it into the tray instead would leave the
    # swap script waiting for a process that is never going to exit.
    server.set_quit_hook(shutdown)

    def show() -> None:
        if not window.open():
            webbrowser.open(url)

    icon = None
    if getattr(settings, "close_to_tray", True) and tray.available():
        icon = tray.Tray(on_open=show, on_quit=shutdown)
        if not icon.start():
            icon = None

    try:
        announced = False
        while True:
            window.wait()
            if finished.is_set() or icon is None:
                break
            if not announced:
                # A window that vanishes with the work still running needs to
                # say where it went, once.
                icon.notify("Conrod is still running",
                            "Any scan carries on. Click here to open it, or "
                            "right-click to quit.")
                announced = True
            while not finished.is_set() and not window.is_open():
                time.sleep(0.25)
            if finished.is_set():
                break
    finally:
        if icon is not None:
            icon.stop()
    return True


INSTANCE_FILE = DATA_ROOT / "instance.json"


def _record_instance(port: int) -> None:
    try:
        INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        INSTANCE_FILE.write_text(
            json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def _forget_instance() -> None:
    try:
        INSTANCE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _live_instance() -> int | None:
    """The port of a Conrod already running here, if one is actually alive.

    The recorded port is checked rather than trusted. A build that was killed,
    crashed or lost power leaves the file behind, and starting a second server
    is better than refusing to start at all because of a stale file.
    """
    try:
        recorded = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        port = int(recorded["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.6)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return None
    except OSError:
        return None

    # Something is listening, but on a machine that reassigns ports it might
    # not be us. Ask it.
    try:
        import httpx

        reply = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0)
        if reply.status_code == 200 and "summary" in reply.json():
            return port
    except Exception:
        return None
    return None


def launch(settings: Settings | None = None, number_map: NumberMap | None = None,
           *, force_browser: bool = False, serve_only: bool = False,
           port: int | None = None) -> int:
    settings = settings or Settings.load()
    server.configure(settings, number_map or NumberMap())

    # Opening Conrod while Conrod is already open used to break both copies.
    # The second one started a server on the next free port, asked Edge for a
    # window, and Edge -- already running that profile -- handed the URL to
    # the existing browser process and returned straight away. There was then
    # nothing to wait on, the second process exited, its server went with it,
    # and the window it had just opened showed "127.0.0.1 refused to connect".
    #
    # So a second launch does not start a second server. It puts a window back
    # on the one already running and gets out of the way, which is also what
    # should happen anyway: one scan at a time, because the GPU is
    # single-tenant.
    if not serve_only and port is None:
        existing = _live_instance()
        if existing is not None:
            running = f"http://127.0.0.1:{existing}"
            print(f"Conrod is already running at {running}.")
            if not force_browser and _open_app_window(running) is not None:
                return 0
            webbrowser.open(running)
            return 0

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

    _record_instance(port)
    try:
        if serve_only:
            print(f"Conrod is serving on {url}")
            try:
                while thread.is_alive():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            return 0

        if not force_browser and _run_windowed(url, settings):
            return 0

        if not force_browser:
            print("No Chromium browser found for an app window; using the "
                  "default browser instead.", file=sys.stderr)
        print(f"Conrod is running at {url}")
        webbrowser.open(url)
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        # A crash leaves this behind, which is why _live_instance checks the
        # port rather than believing the file.
        _forget_instance()
