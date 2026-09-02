"""Desktop window.

Runs the local server on a free port and shows it in a native window via
pywebview, which uses the WebView2 runtime already present on Windows 11. If
pywebview is missing the app still runs — it just opens in the default browser
instead, which is also what happens when the exe is launched with --browser.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import server
from .config import Settings
from .mapping import NumberMap


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


class _Api:
    """Exposed to the page as window.pywebview.api.

    Only exists to give the folder picker a real native dialog; everything
    else goes through HTTP so the browser fallback behaves identically.
    """

    def __init__(self):
        self.window = None

    def pick_folder(self) -> str | None:
        import webview

        if not self.window:
            return None
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else str(result)


def launch(settings: Settings | None = None, number_map: NumberMap | None = None,
           *, force_browser: bool = False, port: int | None = None) -> int:
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

    if not force_browser:
        try:
            import webview

            api = _Api()
            api.window = webview.create_window(
                "Conrod", url, width=1440, height=920,
                min_size=(1024, 700), js_api=api,
            )
            webview.start()
            return 0
        except ImportError:
            pass
        except Exception as exc:
            print(f"Native window unavailable ({exc}); opening a browser.",
                  file=sys.stderr)

    print(f"Conrod is running at {url}")
    webbrowser.open(url)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0
