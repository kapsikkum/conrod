"""Text reading with RapidOCR.

RapidOCR is PaddleOCR's detection and recognition models running on
onnxruntime, which matters here because it means no PaddlePaddle, no compiler,
and a dependency the packaged executable can actually carry.

Two things learned the hard way and encoded below:

- Its text detector rescales the input before looking for text, so running it
  over a whole car finds nothing at all. It must be given a crop in which the
  text is already large — a plate box, or a tightly framed vehicle.
- Recognition scores come back as strings, not floats.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .config import Settings

_engine = None
_engine_lock = threading.Lock()

# Characters OCR routinely swaps for digits on racing liveries.
_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1",
                             "S": "5", "B": "8", "Z": "2", "G": "6"})

_STRIP = re.compile(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$")


@dataclass
class Reading:
    number: str | None
    confidence: float
    source: str = "ocr"


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        return _engine


def ocr_lines(image: Image.Image | Path, settings: Settings) -> list[tuple[str, float]]:
    """Every text line the engine can read, as (text, confidence) pairs."""
    engine = _get_engine()
    try:
        if isinstance(image, Path):
            result, _elapsed = engine(str(image))
        else:
            import numpy as np

            result, _elapsed = engine(np.asarray(image.convert("RGB")))
    except Exception:
        return []

    lines: list[tuple[str, float]] = []
    for _box, text, score in (result or []):
        try:
            lines.append((str(text), float(score)))
        except (TypeError, ValueError):
            continue
    return lines


def ocr_tokens(image: Image.Image | Path, settings: Settings) -> list[tuple[str, float, float]]:
    """Text with the relative area of each box, for ranking by prominence."""
    engine = _get_engine()
    try:
        if isinstance(image, Path):
            result, _elapsed = engine(str(image))
        else:
            import numpy as np

            result, _elapsed = engine(np.asarray(image.convert("RGB")))
    except Exception:
        return []

    tokens: list[tuple[str, float, float]] = []
    for box, text, score in (result or []):
        try:
            tokens.append((str(text), float(score), _box_area(box)))
        except (TypeError, ValueError):
            continue
    return tokens


def read_number(crop_path: Path | Image.Image, settings: Settings) -> Reading:
    """The most plausible competition number in a vehicle crop."""
    best: tuple[float, str] | None = None
    for text, score, area in ocr_tokens(crop_path, settings):
        candidate = _normalise(text)
        if not _plausible(candidate, settings):
            continue
        # The race number is almost always the largest lettering on the car, so
        # weight recognition confidence by how much of the crop the text fills.
        rank = score * (1.0 + area)
        if best is None or rank > best[0]:
            best = (rank, candidate)

    if best is None:
        return Reading(None, 0.0)
    return Reading(best[1], min(1.0, best[0]))


def visible_text(image: Image.Image, settings: Settings,
                 exclude: set[str] | None = None) -> list[str]:
    """Readable text on a vehicle: sponsors, team names, badges.

    Short fragments and anything already accounted for (the plate, the race
    number) are dropped so the keyword list stays worth reading.
    """
    exclude = {e.upper() for e in (exclude or set())}
    seen: set[str] = set()
    out: list[str] = []
    for text, score, _area in ocr_tokens(image, settings):
        cleaned = _STRIP.sub("", text).strip()
        if not cleaned or score < settings.text_min_confidence:
            continue
        if len(cleaned) < settings.text_min_length:
            continue
        key = re.sub(r"[^A-Z0-9]", "", cleaned.upper())
        if not key or key in exclude or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out[: settings.max_text_items]


def _normalise(text: str) -> str:
    token = _STRIP.sub("", (text or "").strip())
    if not token:
        return ""
    # Only bend lookalike letters into digits when the token is mostly numeric
    # already, so a sponsor word like "BOSS" is not read as "8055".
    digits = sum(c.isdigit() for c in token)
    if digits and digits >= len(token) - 1:
        token = token.translate(_LOOKALIKES)
    return token


def _plausible(token: str, settings: Settings) -> bool:
    if not token or not token.isdigit():
        return False
    if not (settings.number_min_len <= len(token) <= settings.number_max_len):
        return False
    # A leading zero is real in some series ("07"), but two is a misread decal.
    if len(token) > 1 and token.startswith("00"):
        return False
    return True


def _box_area(box) -> float:
    """Normalised-ish area of an OCR quad, guarding against odd shapes."""
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return abs(max(xs) - min(xs)) * abs(max(ys) - min(ys)) / 1_000_000.0
    except Exception:
        return 0.0
