"""Prove a build actually works.

Importing a module is not evidence that it runs. Release 0.1.0 imported
cleanly, started, and then failed every detection with "No module named
'sympy'" because the spec excluded a package torch loads at import time, and
crashed outright in windowed mode because uvicorn called ``.isatty()`` on a
stdout that was None. Neither is visible without executing the real paths.

So this exercises them: a genuine YOLO inference, a genuine OCR pass, a
genuine plate-detector load. It is wired into CI and can be run against an
unpacked release with ``Conrod.exe --selftest``.

Checks that need a separate install (ExifTool, Ollama) report as SKIP rather
than FAIL — they are not part of what the build is responsible for.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

from PIL import Image, ImageDraw

from .config import CACHE_DIR, LOG_PATH, Settings, bundle_dir, find_exiftool

OK, FAIL, SKIP = "PASS", "FAIL", "SKIP"
NL = chr(10)


def _synthetic_scene() -> Image.Image:
    """A crude car-ish blob on a road. Enough for a detector to have an
    opinion about; we care that inference runs, not that it finds a car."""
    image = Image.new("RGB", (960, 640), (150, 160, 170))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 430, 960, 640), fill=(70, 70, 75))
    draw.rounded_rectangle((250, 300, 700, 470), radius=30, fill=(30, 60, 140))
    draw.rounded_rectangle((330, 250, 620, 320), radius=20, fill=(20, 40, 90))
    draw.ellipse((300, 430, 380, 510), fill=(20, 20, 20))
    draw.ellipse((570, 430, 650, 510), fill=(20, 20, 20))
    return image


def _check_web_assets() -> tuple[str, str]:
    root = bundle_dir() / "conrod" / "web"
    wanted = ["index.html", "style.css", "app.js"]
    missing = [n for n in wanted if not (root / n).is_file()]
    if missing:
        return FAIL, f"missing {', '.join(missing)} under {root}"
    return OK, f"{len(wanted)} files at {root}"


def _check_store() -> tuple[str, str]:
    from . import store

    conn = store.connect()
    try:
        conn.execute("SELECT count(*) FROM jobs").fetchone()
        conn.execute("SELECT count(*) FROM detections").fetchone()
    finally:
        conn.close()
    return OK, "schema opens and queries"


def _check_torchvision_ops() -> tuple[str, str]:
    """torchvision's compiled operators, which ultralytics needs for NMS.

    Split out from the detector check because it fails in a way that reads
    like a model problem and is not one. v0.2.0 built against torchvision
    0.29, PyInstaller left _C.pyd out of the bundle with only a warning, and
    every detection died on "operator torchvision::nms does not exist".
    """
    import torch
    import torchvision

    if not hasattr(torch.ops.torchvision, "nms"):
        raise RuntimeError(
            "torchvision's operators are not registered -- _C is missing from "
            "the bundle, so no detection can run"
        )
    return OK, f"torchvision {torchvision.__version__} nms registered"


def _check_detector(settings: Settings) -> tuple[str, str]:
    """The one that catches a torch dependency pruned out of the bundle."""
    from . import detect

    scene = CACHE_DIR / "_selftest_scene.jpg"
    _synthetic_scene().save(scene, quality=90)
    started = time.monotonic()
    found = detect.detect(scene, settings)
    elapsed = time.monotonic() - started
    scene.unlink(missing_ok=True)
    return OK, f"inference ran in {elapsed:.1f}s, {len(found)} detections"


def _check_ocr(settings: Settings) -> tuple[str, str]:
    """Catches RapidOCR's ONNX models not being collected into the bundle."""
    from . import ocr

    card = Image.new("RGB", (420, 140), (255, 255, 255))
    ImageDraw.Draw(card).text((30, 45), "CONROD 88", fill=(0, 0, 0))
    lines = ocr.ocr_lines(card, settings)
    read = ", ".join(text for text, _ in lines) or "(nothing legible)"
    return OK, f"engine ran, read {read}"


def _check_plates(settings: Settings) -> tuple[str, str]:
    from . import plates

    plates.find_plates(_synthetic_scene(), settings)
    return OK, f"{settings.plate_model} loaded and ran"


def _check_plate_reader(settings: Settings) -> tuple[str, str]:
    """Loads its own ONNX model, so a frozen build can break it on its own."""
    from . import plates
    from PIL import Image as _Image

    if not settings.plate_reader:
        return SKIP, "turned off in settings"
    card = _Image.new("RGB", (280, 90), (245, 245, 245))
    ImageDraw.Draw(card).text((40, 35), "73111J", fill=(10, 10, 40))
    text, conf = plates._read_plate_text(card, settings)
    return OK, (f"{settings.plate_reader_model} ran"
                + (f", read {text} at {conf:.2f}" if text else ", read nothing"))


def _check_exiftool() -> tuple[str, str]:
    try:
        exe = find_exiftool()
    except RuntimeError:
        return SKIP, "not on PATH (a separate install, not shipped)"
    if not exe:
        return SKIP, "not on PATH (a separate install, not shipped)"
    return OK, str(exe)


def _check_window() -> tuple[str, str]:
    """Is there a browser that can host a chromeless app window?

    This used to ask whether pywebview imported, and reported PASS on the
    0.1.1 build whose window silently fell back to a browser tab. Check the
    thing that is actually used.
    """
    from .desktop import _browser_binaries

    found = _browser_binaries()
    if not found:
        return FAIL, "no Edge/Chrome/Brave found; UI would open in a browser tab"
    return OK, f"{found[0].name} will host the window"


def run() -> int:
    settings = Settings.load()
    checks = [
        ("web assets", _check_web_assets),
        ("job store", _check_store),
        ("torchvision ops", _check_torchvision_ops),
        ("vehicle detector", lambda: _check_detector(settings)),
        ("text reader", lambda: _check_ocr(settings)),
        ("plate detector", lambda: _check_plates(settings)),
        ("plate reader", lambda: _check_plate_reader(settings)),
        ("native window", _check_window),
        ("exiftool", _check_exiftool),
    ]

    frozen = getattr(sys, "frozen", False)
    report: list[str] = []

    def say(line: str = "") -> None:
        report.append(line)
        print(line, flush=True)

    say(f"Conrod self-test ({'frozen build' if frozen else 'source'}, "
        f"Python {sys.version.split()[0]})")
    say()

    failures = 0
    for name, check in checks:
        try:
            status, detail = check()
        except Exception as exc:
            status = FAIL
            detail = f"{type(exc).__name__}: {exc}"
            report.append(traceback.format_exc())
        if status == FAIL:
            failures += 1
        say(f"  {status}  {name:<18} {detail}")

    say()
    say(f"{failures} check(s) failed." if failures else "All checks passed.")

    # A frozen build may have attached to a console that CI cannot capture, or
    # to none at all. The log is the copy that can always be retrieved.
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(NL.join(report) + NL)
    except Exception:
        pass

    return 1 if failures else 0
