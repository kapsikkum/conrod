"""Hybrid number reading: cheap OCR first, VLM only where it is needed."""

from __future__ import annotations

from pathlib import Path

import httpx

from . import ocr, vlm
from .config import Settings
from .ocr import Reading


def read_number(crop_path: Path, settings: Settings,
                client: httpx.Client | None = None) -> Reading:
    """Read one vehicle crop, escalating to the VLM when OCR is unconvincing."""
    first = ocr.read_number(crop_path, settings)
    if first.number and first.confidence >= settings.ocr_accept_confidence:
        return first

    if not settings.use_vlm:
        return first

    second = vlm.read_number(crop_path, settings, client=client)
    if second.number:
        # When both engines independently land on the same number, that
        # agreement is worth more than either one's own confidence.
        if first.number == second.number:
            return Reading(second.number, min(1.0, second.confidence + 0.15), "ocr+vlm")
        return second

    # VLM declined. Fall back to a weak OCR guess rather than nothing, but keep
    # its low confidence so the review UI surfaces it for a human.
    return first
