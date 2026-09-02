"""Vehicle detection.

Runs a COCO-pretrained YOLO over the frame and keeps the classes that
correspond to subject vehicles. Detection stays on the CPU on purpose: the box
model is cheap and spreads across cores, while the VRAM is worth more to the
vision-language model, which is orders of magnitude slower per item.

Two findings from real trackside frames shape this module:

- **imgsz 960 beats 1600.** Measured over fourteen Mount Panorama panners,
  960 found 13/14 subjects against 1600's 12/14, with higher confidence and at
  40% of the cost. The network was trained at 640, and a distant car scaled to
  960 sits closer to that than one scaled to 1600.
- **The raw box is not safe to crop to.** On a close car portrait YOLO returned
  a box that cut the entire nose off the car, taking the registration plate
  with it. Boxes are therefore padded generously, and a vehicle that dominates
  the frame is analysed as the whole frame instead.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image

from .config import BIKE_CLASSES, CACHE_DIR, MODEL_DIR, Settings, VEHICLE_CLASSES

_model = None
_model_lock = threading.Lock()

Box = tuple[float, float, float, float]


@dataclass
class Detection:
    box: Box                    # the detector's own box, frame pixels
    crop_box: Box               # padded box actually used for analysis
    cls: str
    conf: float
    class_id: int = 2
    crop_path: Path | None = None

    @property
    def is_bike(self) -> bool:
        return self.class_id in BIKE_CLASSES

    @property
    def area(self) -> float:
        return (self.box[2] - self.box[0]) * (self.box[3] - self.box[1])


def load_model(settings: Settings):
    """Load YOLO once per process, caching the weights under the data root."""
    global _model
    with _model_lock:
        if _model is not None:
            return _model
        from ultralytics import YOLO  # heavy import, kept lazy

        weights = MODEL_DIR / settings.detect_model
        if not weights.exists():
            # Ultralytics downloads into the CWD by default, so fetch and move.
            YOLO(settings.detect_model)
            stray = Path(settings.detect_model)
            if stray.exists():
                stray.replace(weights)
        _model = YOLO(str(weights))
        return _model


def detect(image_path: Path, settings: Settings) -> list[Detection]:
    """Find subject vehicles in one frame."""
    model = load_model(settings)
    results = model.predict(
        source=str(image_path),
        imgsz=settings.detect_imgsz,
        conf=settings.detect_conf,
        classes=settings.active_classes(),
        verbose=False,
        device="cpu",
    )
    if not results:
        return []

    result = results[0]
    height, width = result.orig_shape
    min_edge = min(width, height) * settings.min_box_fraction

    found: list[Detection] = []
    for box in result.boxes:
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
        # A distant car in the background is not the subject and nothing on it
        # is legible; dropping these is what keeps the run time sane.
        if max(x2 - x1, y2 - y1) < min_edge:
            continue
        class_id = int(box.cls[0])
        found.append(
            Detection(
                box=(x1, y1, x2, y2),
                crop_box=expand_box((x1, y1, x2, y2), width, height, settings),
                cls=VEHICLE_CLASSES.get(class_id, "vehicle"),
                conf=float(box.conf[0]),
                class_id=class_id,
            )
        )

    found.sort(key=lambda d: d.area, reverse=True)
    found = _drop_contained(found)
    return found[: settings.max_vehicles_per_frame]


def expand_box(box: Box, width: int, height: int, settings: Settings) -> Box:
    """Pad a detector box out to something safe to analyse.

    Padding is proportional to the box, so a small distant car gains a little
    context and a large near one gains a lot — which is where the clipping
    problem actually bites. Once a vehicle covers most of the frame the box is
    the unreliable part, so the whole frame is used.
    """
    x1, y1, x2, y2 = box
    frame_area = float(width * height) or 1.0
    if ((x2 - x1) * (y2 - y1)) / frame_area >= settings.dominant_subject_fraction:
        return (0.0, 0.0, float(width), float(height))

    pad_x = (x2 - x1) * settings.crop_padding
    pad_y = (y2 - y1) * settings.crop_padding
    return (
        max(0.0, x1 - pad_x), max(0.0, y1 - pad_y),
        min(float(width), x2 + pad_x), min(float(height), y2 + pad_y),
    )


def _drop_contained(detections: list[Detection]) -> list[Detection]:
    """Remove boxes swallowed by a larger one.

    A motorcycle and its rider often produce an overlapping 'car' box, and a
    pack shot can nest a distant car inside a nearer one's padded box. Keeping
    both would analyse and keyword the same subject twice.
    """
    kept: list[Detection] = []
    for det in detections:                      # already largest-first
        redundant = False
        for bigger in kept:
            if _overlap_fraction(det.box, bigger.box) > 0.75:
                redundant = True
                break
        if not redundant:
            kept.append(det)
    return kept


def _overlap_fraction(inner: Box, outer: Box) -> float:
    """How much of `inner` lies inside `outer`."""
    ix1 = max(inner[0], outer[0])
    iy1 = max(inner[1], outer[1])
    ix2 = min(inner[2], outer[2])
    iy2 = min(inner[3], outer[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    inner_area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return intersection / inner_area if inner_area else 0.0


def cut(frame: Image.Image, detection: Detection, settings: Settings) -> Image.Image:
    """The analysis crop: native resolution, capped only at the top end.

    Plate detection and OCR both need real pixels, so this is deliberately not
    downscaled to the size the vision model wants — that happens later, per
    consumer.
    """
    x1, y1, x2, y2 = detection.crop_box
    crop = frame.convert("RGB").crop((int(x1), int(y1), int(x2), int(y2)))
    if crop.width < 2 or crop.height < 2:
        return crop

    longest = max(crop.size)
    if longest < settings.crop_min_edge:
        scale = settings.crop_min_edge / longest
    elif longest > settings.crop_max_edge:
        scale = settings.crop_max_edge / longest
    else:
        return crop
    return crop.resize(
        (max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
        Image.LANCZOS,
    )


def write_crops(image_path: Path, detections: Sequence[Detection],
                settings: Settings, key: str) -> None:
    """Cache each detection's crop as a JPEG for the reader and review UI."""
    crop_dir = CACHE_DIR / "crops" / key[:2] / key
    crop_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(image_path) as frame:
        frame.load()
        for index, det in enumerate(detections):
            crop = cut(frame, det, settings)
            out = crop_dir / f"{index:02d}.jpg"
            crop.save(out, "JPEG", quality=92)
            det.crop_path = out
