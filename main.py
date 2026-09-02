#!/usr/bin/env python
"""Application entry point.

This is what PyInstaller freezes. Keeping it separate from the package means
the frozen build and a `python main.py` run take exactly the same path.
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def _attach_parent_console() -> bool:
    """Borrow the console of whatever launched us, if there is one.

    A windowed build has no console of its own, so ``Conrod.exe --selftest``
    run from a terminal would print into the void. Attaching to the parent's
    console puts the output where the person who typed the command is looking.
    Returns False when launched from Explorer, where there is no parent
    console to attach to.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ATTACH_PARENT_PROCESS = -1
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False
        for name in ("stdout", "stderr", "__stdout__", "__stderr__"):
            if getattr(sys, name, None) is None:
                setattr(sys, name, open("CONOUT$", "w", encoding="utf-8",
                                        errors="replace", buffering=1))
        return True
    except Exception:
        return False


def _ensure_streams() -> None:
    """Give a windowed build somewhere to write.

    A PyInstaller build with ``console=False`` has no console attached, so
    ``sys.stdout`` and ``sys.stderr`` are None. Anything that touches them
    dies before the window ever appears: uvicorn's logging config calls
    ``.isatty()`` on stdout, and Ultralytics' first-run weight download writes
    a progress bar to it. Both crashed the 0.1.0 build.

    Prefer the launching terminal; fall back to a log file rather than a null
    sink, because when a packaged app fails there is otherwise nothing at all
    to go on.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    if _attach_parent_console():
        return

    stream = None
    try:
        from conrod.config import LOG_PATH

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stream = open(LOG_PATH, "a", encoding="utf-8", errors="replace",
                      buffering=1)
    except Exception:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            return

    for name in ("stdout", "stderr", "__stdout__", "__stderr__"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, stream)


def main() -> int:
    # Frozen Windows builds re-exec themselves for each child process; without
    # this a packaged app spawns copies of its own window.
    multiprocessing.freeze_support()
    _ensure_streams()

    if "--selftest" in sys.argv:
        from conrod.selftest import run as selftest

        return selftest()

    from conrod.config import Settings
    from conrod.desktop import launch
    from conrod.mapping import NumberMap

    force_browser = "--browser" in sys.argv
    if "--cli" in sys.argv:
        from cli import main as cli_main

        sys.argv.remove("--cli")
        return cli_main()

    settings = Settings.load()
    number_map = NumberMap()
    if settings.extra.get("map_path"):
        try:
            number_map = NumberMap.load(settings.extra["map_path"])
        except Exception:
            pass
    return launch(settings, number_map, force_browser=force_browser)


if __name__ == "__main__":
    raise SystemExit(main())
