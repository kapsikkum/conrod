"""Registration plate detection and reading.

Two stages, because neither works alone. A 7.5 MB YOLOv9 ONNX model locates the
plate — measured at ~60 ms on a 6960x4640 frame — and the plate crop is then
read at native resolution by OCR. Running OCR over a whole car finds nothing:
its text detector rescales the image and a plate that is 800 px wide in the
original becomes unreadable.

Plate *detection* is region-agnostic; what is Australian here is the format
validation applied to the characters afterwards.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Settings

_detector = None
_detector_lock = threading.Lock()

# Australian state and territory badging, as OCR tends to render it.
STATES = {
    "NSW": "New South Wales", "VIC": "Victoria", "QLD": "Queensland",
    "SA": "South Australia", "WA": "Western Australia", "TAS": "Tasmania",
    "NT": "Northern Territory", "ACT": "Australian Capital Territory",
}
_STATE_HINTS = {
    "NEWSOUTHWALES": "NSW", "VICTORIA": "VIC", "QUEENSLAND": "QLD",
    "SOUTHAUSTRALIA": "SA", "WESTERNAUSTRALIA": "WA", "TASMANIA": "TAS",
    "NORTHERNTERRITORY": "NT", "AUSTRALIANCAPITALTERRITORY": "ACT",
}

# The common issue formats. A personalised plate matches none of these and is
# still perfectly valid, so these raise confidence rather than gate acceptance.
_FORMATS = [
    re.compile(r"^[A-Z]{3}\d{2}[A-Z]$"),   # NSW current, e.g. FD23RS
    re.compile(r"^[A-Z]{3}\d{3}$"),        # widespread older issue
    re.compile(r"^\d{3}[A-Z]{3}$"),        # QLD older
    re.compile(r"^[A-Z]\d{3}[A-Z]{2}$"),   # VIC
    re.compile(r"^[A-Z]{2}\d{2}[A-Z]{2}$"),
    re.compile(r"^\d{1,3}[A-Z]{1,3}\d{1,3}$"),
]

# Text that shows up on or beside plates and is never the registration itself.
_PLATE_NOISE = {
    "AUSTRALIA", "THEFIRSTSTATE", "SUNSHINESTATE", "GARDENSTATE",
    "THEPLACETOBE", "HOLIDAYISLE", "OUTBACKAUSTRALIA", "THEFESTIVALSTATE",
    "PREMIERSTATE", "THEHEARTOFAUSTRALIA",
}


@dataclass
class PlateReading:
    text: str | None = None
    state: str | None = None
    confidence: float = 0.0
    box: tuple[int, int, int, int] | None = None   # in crop pixel coords
    candidates: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        if not self.text:
            return ""
        return f"{self.text} ({self.state})" if self.state else self.text


def _get_detector(settings: Settings):
    global _detector
    with _detector_lock:
        if _detector is None:
            from open_image_models import LicensePlateDetector

            _detector = LicensePlateDetector(
                detection_model=settings.plate_model,
            )
        return _detector


def find_plates(image: Image.Image, settings: Settings) -> list[tuple[tuple[int, int, int, int], float]]:
    """Locate plates in a vehicle crop. Returns (box, confidence) pairs."""
    detector = _get_detector(settings)
    # The detector wants BGR, the way OpenCV would have loaded it.
    array = np.asarray(image.convert("RGB"))[:, :, ::-1]
    try:
        results = detector.predict(np.ascontiguousarray(array))
    except Exception:
        return []

    found = []
    for result in results:
        if float(result.confidence) < settings.plate_conf:
            continue
        box = result.bounding_box
        found.append((
            (int(box.x1), int(box.y1), int(box.x2), int(box.y2)),
            float(result.confidence),
        ))
    found.sort(key=lambda item: item[1], reverse=True)
    return found[: settings.max_plates_per_vehicle]


def read_plate(image: Image.Image, settings: Settings) -> PlateReading:
    """Find and read the most convincing plate on a vehicle crop."""
    return scan_regions(image, settings)[0]


def scan_regions(image: Image.Image, settings: Settings
                 ) -> tuple[PlateReading, list[tuple[str, float]]]:
    """Read every plate-shaped region, returning the plate and any numbers.

    The plate detector fires on competition number roundels as well as
    registrations — on a Mini at Mount Panorama it boxed the door roundel at
    0.81 confidence. That is a gift rather than a false positive: a tight,
    upscaled crop of the roundel gives a far better number read than OCR across
    the whole car ever does, so both interpretations are returned and the
    caller decides which it wanted.
    """
    from .ocr import ocr_lines  # local import keeps the OCR engine lazy

    candidates = find_plates(image, settings)
    if not candidates:
        return PlateReading(), []

    best = PlateReading()
    numbers: list[tuple[str, float]] = []

    for box, detection_conf in candidates:
        crop = _crop_plate(image, box, settings)
        lines = ocr_lines(crop, settings)

        reading = _interpret(lines, settings)
        reading.box = box
        # Weight the character read by how sure the detector was it is a plate.
        reading.confidence *= 0.5 + 0.5 * detection_conf
        if reading.text and reading.confidence > best.confidence:
            best = reading

        for raw, score in lines:
            token = re.sub(r"[^0-9]", "", raw)
            if not token or token != re.sub(r"[^A-Za-z0-9]", "", raw):
                continue  # mixed letters and digits is a registration, not a number
            if not (settings.number_min_len <= len(token) <= settings.number_max_len):
                continue
            numbers.append((token, float(score) * (0.5 + 0.5 * detection_conf)))

    numbers.sort(key=lambda item: item[1], reverse=True)
    return best, numbers


def _crop_plate(image: Image.Image, box: tuple[int, int, int, int],
                settings: Settings) -> Image.Image:
    """Cut the plate out with a little margin and upscale it for OCR."""
    x1, y1, x2, y2 = box
    pad_x = (x2 - x1) * 0.06
    pad_y = (y2 - y1) * 0.18
    crop = image.crop((
        max(0, int(x1 - pad_x)), max(0, int(y1 - pad_y)),
        min(image.width, int(x2 + pad_x)), min(image.height, int(y2 + pad_y)),
    ))
    if crop.width < 10 or crop.height < 6:
        return crop
    longest = max(crop.size)
    if longest < settings.plate_ocr_edge:
        scale = settings.plate_ocr_edge / longest
        crop = crop.resize(
            (max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
            Image.LANCZOS,
        )
    return crop


def _interpret(lines: list[tuple[str, float]], settings: Settings) -> PlateReading:
    """Pick the registration out of the several strings a plate carries.

    A plate crop typically yields the registration plus a state name and often
    a tourism slogan, so the job is choosing among them rather than reading.
    """
    reading = PlateReading()
    best_score = 0.0

    for raw, score in lines:
        token = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
        if not token:
            continue

        # State badging: record it, but it is never the registration.
        if token in STATES:
            reading.state = token
            continue
        if token in _STATE_HINTS:
            reading.state = _STATE_HINTS[token]
            continue
        if token in _PLATE_NOISE:
            continue

        if not (settings.plate_min_len <= len(token) <= settings.plate_max_len):
            continue
        # A registration is not all letters and not all digits in any Australian
        # issue format; that filter alone removes most slogan fragments.
        if token.isalpha() or token.isdigit():
            if not any(fmt.match(token) for fmt in _FORMATS):
                continue

        token = _trim_to_format(token)
        reading.candidates.append(token)
        weight = float(score)
        if any(fmt.match(token) for fmt in _FORMATS):
            weight = min(1.0, weight + 0.2)
        if weight > best_score:
            best_score, reading.text, reading.confidence = weight, token, weight

    return reading


def _trim_to_format(token: str) -> str:
    """Drop a stray edge character when doing so reveals a valid plate.

    OCR picks up mounting bolts and frame edges as characters: a NSW plate
    reading LA93NG came back as "ELA93NG". Since the trimmed form matches a
    real issue format and the untrimmed one matches nothing, the trim is
    almost certainly right. Only ever removes one character, and only when it
    turns a non-matching token into a matching one.
    """
    if any(fmt.match(token) for fmt in _FORMATS):
        return token
    for candidate in (token[1:], token[:-1]):
        if len(candidate) >= 5 and any(fmt.match(candidate) for fmt in _FORMATS):
            return candidate
    return token


def looks_like_plate(token: str) -> bool:
    """Used elsewhere to avoid mistaking a registration for a race number."""
    token = token.upper()
    return any(fmt.match(token) for fmt in _FORMATS)
