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
_reader = None
_reader_lock = threading.Lock()


def _quieten_onnx() -> None:
    """Stop ONNX Runtime narrating into the activity log.

    Both plate models emit a page of "Error merging shape info ... falling
    back to lenient merge" warnings the first time they load. The lenient
    merge is the correct outcome and the models read plates fine, but at the
    default severity it buries everything the log window is actually for.
    """
    try:
        import onnxruntime

        onnxruntime.set_default_logger_severity(3)   # errors only
    except Exception:
        pass


def _get_reader(settings: Settings):
    """A recogniser trained on plates, rather than general-purpose OCR.

    Measured over 18 plate crops from the Bathurst set: general OCR read 5 of
    them and only at one particular crop padding, this reads all 18 at 0.96+
    and does not care about the padding. It is also about forty times faster
    per crop, which more than pays for the tiled search that finds the plates
    in the first place.
    """
    global _reader
    with _reader_lock:
        if _reader is None:
            _quieten_onnx()
            from fast_plate_ocr import LicensePlateRecognizer

            _reader = LicensePlateRecognizer(settings.plate_reader_model)
        return _reader


def _read_plate_text(crop: Image.Image, settings: Settings) -> tuple[str, float]:
    """Read the characters off a plate-shaped crop. ('', 0.0) if unsure.

    The recogniser always returns something -- fed noise it happily produced
    "8034AC" -- so a confidence floor is not optional here.
    """
    if not settings.plate_reader:
        return "", 0.0
    try:
        prediction = _get_reader(settings).run(
            np.asarray(crop.convert("L")), return_confidence=True)[0]
    except Exception:
        return "", 0.0
    if prediction.char_probs is None:
        return "", 0.0
    confidence = float(np.mean(prediction.char_probs))
    if confidence < settings.plate_reader_min_conf:
        return "", 0.0
    return re.sub(r"[^A-Za-z0-9]", "", prediction.plate).upper(), confidence

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
    # Club and historic registration, which is most of what turns up at a
    # Mount Panorama cruise: five or six digits then a letter, e.g. 73111J
    # on an NSW historic plate. Without this a perfect read was discarded.
    re.compile(r"^\d{4,6}[A-Z]$"),
    re.compile(r"^[A-Z]\d{4,6}$"),
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
            _quieten_onnx()
            from open_image_models import LicensePlateDetector

            _detector = LicensePlateDetector(
                detection_model=settings.plate_model,
            )
        return _detector


def _detect_on(image: Image.Image, settings: Settings
               ) -> list[tuple[tuple[int, int, int, int], float]]:
    """One pass of the plate detector over one image."""
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
    return found


def _tiles(image: Image.Image, edge: int, overlap: float):
    """Overlapping windows covering the image, with the offset of each."""
    step = max(1, int(edge * (1 - overlap)))
    def starts(extent: int) -> list[int]:
        if extent <= edge:
            return [0]
        out = list(range(0, extent - edge + 1, step))
        if out[-1] + edge < extent:
            out.append(extent - edge)
        return out
    for oy in starts(image.height):
        for ox in starts(image.width):
            yield ox, oy, image.crop((ox, oy,
                                      min(ox + edge, image.width),
                                      min(oy + edge, image.height)))


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    if not inter:
        return 0.0
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union else 0.0


def _merge(found: list[tuple[tuple[int, int, int, int], float]]
           ) -> list[tuple[tuple[int, int, int, int], float]]:
    """Keep the most confident box out of each overlapping cluster."""
    kept: list[tuple[tuple[int, int, int, int], float]] = []
    for box, conf in sorted(found, key=lambda item: item[1], reverse=True):
        if all(_iou(box, other) < 0.4 for other, _ in kept):
            kept.append((box, conf))
    return kept


def find_plates(image: Image.Image, settings: Settings) -> list[tuple[tuple[int, int, int, int], float]]:
    """Locate plates and number roundels in a vehicle crop."""
    return _merge(_detect_on(image, settings))[: settings.max_plates_per_vehicle]


def find_plates_native(native: Image.Image, settings: Settings
                       ) -> list[tuple[tuple[int, int, int, int], float]]:
    """Hunt for a small plate in the vehicle at full resolution.

    The analysis crop is capped at 2048px so the vision model has something
    sane to look at, and that downscale is enough to lose a plate: on a Falcon
    at Mount Panorama the plate is 146px wide in the original and a legible
    73111J, but 99px and mush in the crop. No tile size finds it there --
    tried 1100 down to 512 -- while tiling the original finds it at 0.86.

    Only the lower part of the vehicle is scanned. Plates sit on a bumper,
    front or rear; roundels sit higher on the doors but they are large and the
    ordinary whole-crop pass already finds those.
    """
    if not settings.plate_native_search:
        return []

    top = int(native.height * (1.0 - settings.plate_native_lower))
    region = native.crop((0, top, native.width, native.height))

    found = []
    for ox, oy, tile in _tiles(region, settings.plate_tile_edge,
                               settings.plate_tile_overlap):
        for (x1, y1, x2, y2), conf in _detect_on(tile, settings):
            found.append(((x1 + ox, y1 + oy + top, x2 + ox, y2 + oy + top), conf))
    return _merge(found)[: settings.max_plates_per_vehicle]


def read_plate(image: Image.Image, settings: Settings) -> PlateReading:
    """Find and read the most convincing plate on a vehicle crop."""
    return scan_regions(image, settings)[0]


def scan_regions(image: Image.Image, settings: Settings,
                 native: Image.Image | None = None
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

    # (image the candidate lives in, box, detector confidence)
    candidates = [(image, box, conf) for box, conf in find_plates(image, settings)]
    if native is not None:
        candidates += [(native, box, conf)
                       for box, conf in find_plates_native(native, settings)]
    if not candidates:
        return PlateReading(), []

    best = PlateReading()
    numbers: list[tuple[str, float]] = []

    for source, box, detection_conf in candidates:
        # Read the same plate at a couple of paddings and keep the best.
        # How much of the bumper to include changes the answer completely and
        # no single value wins: 0.18 reads the Bathurst set and misses an NSW
        # historic plate, 0.05 does the reverse. OCR on a plate-sized crop is
        # cheap, so try both rather than pick a loser.
        piece = _crop_plate(source, box, settings)

        # The plate recogniser first: it is both better and far cheaper than
        # general OCR on a plate. General OCR still runs, because it is what
        # reads the state name and the competition numbers on roundels, which
        # the plate detector also finds.
        lines = ocr_lines(piece, settings)
        reading = _interpret(lines, settings)

        text, text_conf = _read_plate_text(piece, settings)
        if text and looks_like_plate(text) and text_conf > reading.confidence:
            reading.text, reading.confidence = text, text_conf
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
                settings: Settings, pad_y_frac: float | None = None) -> Image.Image:
    """Cut the plate out with a little margin and upscale it for OCR."""
    x1, y1, x2, y2 = box
    pad_x = (x2 - x1) * settings.plate_pad_x
    pad_y = (y2 - y1) * (settings.plate_pad_y if pad_y_frac is None else pad_y_frac)
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
