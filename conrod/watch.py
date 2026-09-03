"""Watching a folder for frames that arrive after the scan started.

The workflow this exists for: a card goes into the reader and the copy runs
for twenty minutes while the shoot is still being packed up. Starting a scan
afterwards means sitting and waiting for the copy; starting it before means
scanning a third of the shoot and doing the rest by hand.

Nothing here scans anything. It answers one question -- which files in the
folder are new and finished being written -- and the existing resume path
does the rest, because a watch is exactly a resume that happens on its own.

The settling rule is the whole difficulty. A 60MB CR3 being copied exists on
disk, has a name, matches the suffix filter and is *incomplete*: opening it
gets a truncated file or a sharing violation, and the frame is recorded as
broken rather than scanned again later. So a file is only offered once it has
stopped changing, which is a size and mtime that have both held still.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import IMAGE_SUFFIXES

# How long a file has to hold still before it counts as finished. Long enough
# to outlast the stalls in a card copy, short enough that a watch still feels
# like it is keeping up with one.
SETTLE_SECONDS = 20.0

# How often the folder is re-read when nothing is asking it to.
DEFAULT_INTERVAL = 60.0


def key(path) -> str:
    """One spelling of a path, for comparing against what is on record.

    Windows hands the same file back as ``D:\\Work`` and ``d:/work`` depending
    on who was asked. Bursts were silently lost to exactly this once already.
    """
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass
class Sighting:
    """What a file looked like the last time the folder was read."""
    size: int
    mtime: float
    first_seen: float

    def changed(self, size: int, mtime: float) -> bool:
        return size != self.size or mtime != self.mtime


@dataclass
class Watcher:
    """Remembers enough between passes to tell "arrived" from "still arriving"."""

    folder: Path
    recursive: bool = True
    settle: float = SETTLE_SECONDS
    seen: dict[str, Sighting] = field(default_factory=dict)

    def _files(self):
        pattern = "**/*" if self.recursive else "*"
        try:
            for path in self.folder.glob(pattern):
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                    yield path
        except OSError:
            # A folder that went away -- an unplugged card reader, a share
            # that dropped -- is not an error worth stopping a watch for. The
            # next pass will find it again if it comes back.
            return

    def poll(self, known: set[str] | None = None, now: float | None = None) -> list[Path]:
        """Files that are new to us, and have finished being written.

        ``known`` is what the album already holds, so restarting Conrod does
        not offer the whole folder over again.
        """
        now = time.time() if now is None else now
        known = known or set()
        ready: list[Path] = []
        present: set[str] = set()

        for path in self._files():
            present.add(key(path))
            try:
                stat = path.stat()
            except OSError:
                continue                      # vanished between listing and stat
            k = key(path)
            record = self.seen.get(k)
            if record is None:
                # First sighting is never enough. Even a file that is complete
                # has to survive one interval, which costs one pass and buys
                # the guarantee.
                self.seen[k] = Sighting(stat.st_size, stat.st_mtime, now)
                continue
            if record.changed(stat.st_size, stat.st_mtime):
                record.size, record.mtime = stat.st_size, stat.st_mtime
                record.first_seen = now       # the clock restarts on every write
                continue
            if now - record.first_seen < self.settle:
                continue
            if k not in known:
                ready.append(path)

        # Files that have gone are dropped, so a watch left on a working
        # folder all season does not accumulate a record of every frame ever
        # moved out of it. A file that comes back has to settle again, which
        # is the right answer for one that was being rewritten.
        for gone in self.seen.keys() - present:
            del self.seen[gone]

        ready.sort()
        return ready
