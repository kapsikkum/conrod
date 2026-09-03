"""Respecting the cull.

Keywording runs after culling, not before. On a 6000-frame cruise the keepers
are a fraction of the take, and analysing the rejects is hours of GPU time
spent on frames that will never be delivered.

Ratings and flags are read from the sidecar first and the file second, because
that is the order the tools that write them use: Lightroom and Bridge write a
.xmp beside a RAW and leave the RAW untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import JPEG_SUFFIXES, Settings
from .exif import ExifTool

# Lightroom, Bridge and Photo Mechanic all express "rejected" as a negative
# rating in XMP. Capture One and most others use 0-5 with no negatives.
REJECTED = -1


@dataclass
class Cull:
    rating: int = 0
    label: str = ""
    rejected: bool = False

    def passes(self, settings: Settings) -> tuple[bool, str]:
        if settings.skip_rejected and self.rejected:
            return False, "rejected"
        if settings.min_rating > 0 and self.rating < settings.min_rating:
            return False, f"{self.rating} star"
        wanted = (settings.require_label or "").strip().lower()
        if wanted and self.label.lower() != wanted:
            return False, f"label {self.label or 'none'}"
        return True, ""


def sidecar_for(image: Path) -> Path:
    return image.with_suffix(".xmp")


# Frames per exiftool call. One call for the whole shoot is faster in theory
# and unusable in practice: 6,000 frames took about three minutes during which
# the app said nothing at all and looked hung. Chunking gives the scan screen
# something to report without meaningfully slowing the read.
CULL_CHUNK = 200


def read_culls(paths: Sequence[Path], tool: ExifTool,
               on_progress=None) -> dict[Path, Cull]:
    """Ratings and colour labels for a batch of frames."""
    out: dict[Path, Cull] = {}
    if not paths:
        return out

    # Where a sidecar exists it is authoritative — that is where Lightroom
    # records the rating, leaving the RAW itself unrated.
    targets: dict[Path, Path] = {}
    for image in paths:
        sidecar = sidecar_for(image)
        targets[image] = sidecar if (
            image.suffix.lower() not in JPEG_SUFFIXES and sidecar.exists()
        ) else image

    wanted = list(targets.values())
    rows: list[dict] = []
    for start in range(0, len(wanted), CULL_CHUNK):
        rows += tool.read_tags(wanted[start:start + CULL_CHUNK],
                               ["Rating", "Label", "XMP:Rating"])
        if on_progress:
            on_progress(min(start + CULL_CHUNK, len(wanted)), len(wanted))
    by_path = {Path(r.get("SourceFile", "")).resolve(): r for r in rows}

    for image, target in targets.items():
        row = by_path.get(target.resolve(), {})
        rating = row.get("Rating")
        if rating is None:
            rating = row.get("XMP:Rating")
        try:
            value = int(float(rating)) if rating is not None else 0
        except (TypeError, ValueError):
            value = 0
        out[image] = Cull(
            rating=max(0, value),
            label=str(row.get("Label") or ""),
            rejected=value <= REJECTED,
        )
    return out


def filter_frames(paths: Sequence[Path], settings: Settings,
                  tool: ExifTool,
                  on_progress=None) -> tuple[list[Path], dict[str, int]]:
    """Split a scan list into what will be analysed and why the rest was not."""
    if not settings.respect_culling:
        return list(paths), {}

    culls = read_culls(paths, tool, on_progress)
    keep: list[Path] = []
    skipped: dict[str, int] = {}
    for image in paths:
        ok, reason = culls.get(image, Cull()).passes(settings)
        if ok:
            keep.append(image)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    return keep, skipped
