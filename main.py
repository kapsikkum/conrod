#!/usr/bin/env python
"""Application entry point.

This is what PyInstaller freezes. Keeping it separate from the package means
the frozen build and a `python main.py` run take exactly the same path.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Frozen Windows builds re-exec themselves for each child process; without
    # this a packaged app spawns copies of its own window.
    multiprocessing.freeze_support()

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
