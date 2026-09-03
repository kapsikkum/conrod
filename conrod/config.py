"""Paths and tunables.

Code lives in the project directory, but anything big and churny — model
weights, extracted previews, the job database — lives under the user profile so
OneDrive does not try to sync it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _data_root() -> Path:
    # Deliberately NOT %LOCALAPPDATA%: when the tool is driven from a sandboxed
    # or Store-packaged host, that variable is redirected into a per-app
    # container, and the venv and models land somewhere an ordinary shell
    # cannot see. The user profile is not virtualised.
    override = os.environ.get("CONROD_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".conrod"


DATA_ROOT = _data_root()
MODEL_DIR = DATA_ROOT / "models"
CACHE_DIR = DATA_ROOT / "cache"
DB_PATH = DATA_ROOT / "conrod.db"
SETTINGS_PATH = DATA_ROOT / "settings.json"
# A windowed build has no console, so this is the only place a crash before the
# window opens can leave a trace. See _ensure_streams in main.py.
LOG_PATH = DATA_ROOT / "conrod.log"

for _d in (MODEL_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def bundle_dir() -> Path:
    """Where read-only resources live, whether frozen by PyInstaller or not."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


RAW_SUFFIXES = {".cr3", ".cr2", ".crw", ".arw", ".nef", ".raf", ".orf", ".rw2", ".dng"}
JPEG_SUFFIXES = {".jpg", ".jpeg"}
IMAGE_SUFFIXES = RAW_SUFFIXES | JPEG_SUFFIXES

# COCO class ids that count as a subject vehicle.
VEHICLE_CLASSES = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}
BIKE_CLASSES = {3}


def find_exiftool() -> str:
    """Locate exiftool, preferring a copy shipped beside the application."""
    for candidate in (
        bundle_dir() / "bin" / "exiftool.exe",
        bundle_dir() / "exiftool.exe",
    ):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("exiftool")
    if not found:
        raise RuntimeError(
            "ExifTool not found. Install it from https://exiftool.org/ or place "
            "exiftool.exe in the application's bin folder."
        )
    return found


@dataclass
class Settings:
    """Everything tunable in one place.

    Persisted to settings.json so the desktop app's Settings screen and the
    command line agree about what a run will do.
    """

    # --- vehicle detection ---
    detect_model: str = "yolo11s.pt"
    detect_imgsz: int = 960
    detect_conf: float = 0.25
    # A vehicle smaller than this fraction of the frame's short edge is
    # background traffic, not the subject.
    min_box_fraction: float = 0.08
    max_vehicles_per_frame: int = 8
    include_cars: bool = True
    include_bikes: bool = True
    include_trucks: bool = True

    # --- crop geometry ---
    # Generous by default. A tight box routinely clips the nose off a car and
    # takes the plate with it, which costs far more than a little background.
    crop_padding: float = 0.18
    # When one vehicle fills this much of the frame, treat the whole frame as
    # the crop: the detector's box is the unreliable part at that scale.
    dominant_subject_fraction: float = 0.45
    crop_min_edge: int = 320
    crop_max_edge: int = 2048

    # --- plates ---
    read_plates: bool = True
    plate_model: str = "yolo-v9-t-640-license-plate-end2end"
    plate_conf: float = 0.35
    plate_ocr_edge: int = 700
    plate_pad_x: float = 0.06
    # How much to include above and below the plate. There is no single right
    # answer: measured over the Bathurst set, 0.18 reads four plates and 0.05
    # reads none -- but on an NSW historic plate it is exactly the other way
    # round. Each candidate is therefore read at several paddings and the
    # best-validated result wins; these are the ones tried.
    plate_pad_y: float = 0.18
    # A recogniser trained on plates rather than general OCR. Read all 18 test
    # crops correctly at 0.96+ where general OCR read 5, and 40x faster.
    plate_reader: bool = True
    plate_reader_model: str = "global-plates-mobile-vit-v2-model"
    # It returns a plate for anything, including noise, so this floor matters.
    plate_reader_min_conf: float = 0.75
    # Search the full-resolution vehicle for a small plate, in overlapping
    # tiles, rather than only the downscaled analysis crop. Finds plates that
    # are otherwise lost, and costs roughly a second per vehicle.
    plate_native_search: bool = True
    plate_native_lower: float = 0.55   # bottom fraction of the vehicle to scan
    # 1280 measured best: same plates found as 768 for two thirds of the cost,
    # because each tile is one inference regardless of its size.
    plate_tile_edge: int = 1280
    plate_tile_overlap: float = 0.25
    plate_min_len: int = 2
    plate_max_len: int = 8
    max_plates_per_vehicle: int = 2

    # --- competition numbers ---
    read_numbers: bool = True
    ocr_accept_confidence: float = 0.80
    number_min_len: int = 1
    number_max_len: int = 3

    # --- free text (sponsors, teams, badges) ---
    read_text: bool = True
    text_min_confidence: float = 0.55
    text_min_length: int = 2
    max_text_items: int = 12

    # --- vision-language model ---
    use_vlm: bool = True
    vlm_model: str = "qwen2.5vl:7b"
    vlm_host: str = "http://127.0.0.1:11434"
    vlm_timeout: float = 180.0
    vlm_input_edge: int = 1568
    # After grouping, reconcile a group's disagreeing readings into one
    # canonical name with a text-only call. One call per vehicle, not per
    # frame, and it can only choose among names that were actually read.
    normalise_names: bool = True

    # --- bursts and sharpness ---
    # Which body took each frame and which run of frames it belongs to. Two
    # shooters interleave into one folder, so this is what makes "the same
    # burst" mean anything -- see bursts.py.
    # When a burst cannot agree what the vehicle is, show the model its
    # sharpest frames together rather than trusting any single blurred one.
    burst_second_look: bool = True
    group_by_burst: bool = True
    burst_gap: float = 4.0
    # Where the subject sharpness score lands a frame.
    #
    # Set from 280 real crops of a roadside session rather than guessed. Real
    # photographs score well below synthetic test targets -- a car is mostly
    # smooth painted panel -- so thresholds picked on synthetic images called
    # four per cent of a shoot sharp and forty per cent unusable.
    #
    # Placed by looking at the pictures at each decile. Below 0.25 the
    # subject is genuinely gone; above 0.52 it is crisp. The wide band
    # between them is where the good panning shots live, and calling those
    # anything worse than "soft" would throw away the keepers.
    #
    # Still a per-shooter number: it depends on the lens, the light and how
    # you pan. The raw score is stored beside the verdict, so a shoot can be
    # re-sorted on a new threshold without re-reading a single file.
    sharp_at: float = 0.52
    blurred_below: float = 0.25
    identify_make_model: bool = True
    identify_colour: bool = True
    identify_team: bool = True

    # --- culling ---
    # Keywording happens after the cull. Analysing rejects is GPU time spent on
    # frames that will never be delivered.
    # Group crops that look like the same vehicle and settle on one identity.
    # The vision model answers each crop alone, so one car can come back as
    # four different models across a panning burst.
    group_vehicles: bool = True
    respect_culling: bool = True
    skip_rejected: bool = True
    min_rating: int = 0            # 0 = any; 1-5 = that many stars or better
    require_label: str = ""        # e.g. "Green" to keyword only that label

    # --- writing ---
    write_sidecar_for_raw: bool = True
    # Off by default: a description is a single value, so writing one replaces
    # whatever the photographer put there. Keywords merge; captions do not.
    overwrite_caption: bool = False
    keyword_prefix: str = ""
    write_plate_keyword: bool = True
    write_caption: bool = False

    # --- runtime ---
    # Analysis threads. Plate detection and OCR are onnxruntime calls that
    # release the GIL, so they genuinely overlap; the vision model serialises
    # at Ollama regardless, and queuing a couple of requests keeps the GPU fed
    # while the CPU stages run. More than about four wins nothing on 8 GB.
    analysis_workers: int = 3
    # exiftool is single-threaded Perl, so extraction scales with processes:
    # 4 measured 2.5x faster than 1 on a folder of CR3s.
    preview_workers: int = 4
    detect_workers: int = 2
    workers: int = max(2, (os.cpu_count() or 8) - 2)

    extra: dict = field(default_factory=dict)

    # -- persistence ------------------------------------------------------

    def active_classes(self) -> list[int]:
        """COCO ids to ask the detector for, per the include_* switches."""
        wanted: list[int] = []
        if self.include_cars:
            wanted.append(2)
        if self.include_bikes:
            wanted.append(3)
        if self.include_trucks:
            wanted += [5, 7]
        return wanted or sorted(VEHICLE_CLASSES)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path | None = None) -> None:
        path = path or SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or SETTINGS_PATH
        settings = cls()
        if not path.exists():
            return settings
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return settings
        # Ignore keys from an older or newer build rather than failing to start.
        known = {f.name for f in fields(cls)}
        for key, value in stored.items():
            if key in known:
                setattr(settings, key, value)
        return settings

    def apply(self, updates: dict) -> "Settings":
        known = {f.name for f in fields(type(self))}
        for key, value in updates.items():
            if key in known:
                setattr(self, key, value)
        return self


DEFAULTS = Settings()
