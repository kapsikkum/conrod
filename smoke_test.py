#!/usr/bin/env python
"""End-to-end smoke test that needs no photos of your own.

Exercises: number-map parsing, OCR number normalisation, YOLO detection and
cropping, the Ollama vision reader, XMP sidecar creation and keyword writing,
and the SQLite job store.

Detection runs against the bus photo that ships with Ultralytics, which
contains a real COCO vehicle, so the detector is genuinely exercised rather
than mocked.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from conrod import detect as detect_mod
from conrod import ocr, store, vlm
from conrod.config import Settings
from conrod.exif import ExifTool
from conrod.mapping import NumberMap
from conrod.writer import sidecar_for, write_keywords

PASS, FAIL = "  ok  ", " FAIL "
results: list[tuple[bool, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((condition, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return condition


def test_mapping(tmp: Path) -> None:
    csv_path = tmp / "entries.csv"
    csv_path.write_text(
        "number,driver,team\n"
        "88,Broc Feeney,Triple Eight\n"
        "07,Test Driver,Privateer;Backup Team\n",
        encoding="utf-8",
    )
    mapping = NumberMap.load(csv_path)
    check("map loads rows", len(mapping) == 2, f"{len(mapping)} entries")

    kw = mapping.keywords_for("88")
    check("map expands to keywords",
          {"88", "#88", "Car 88", "Broc Feeney", "Triple Eight"} <= set(kw), str(kw))

    # '07' in the CSV and '7' from the reader must be the same competitor.
    check("leading zeros normalise", "Test Driver" in mapping.keywords_for("7"),
          str(mapping.keywords_for("7")))

    # A cell holding several values splits into separate keywords.
    check("multi-value cells split",
          "Backup Team" in mapping.keywords_for("07"), str(mapping.keywords_for("07")))


def test_number_plausibility() -> None:
    settings = Settings()
    cases = [("42", True), ("7", True), ("123", True), ("1234", False),
             ("0007", False), ("", False), ("ABC", False)]
    ok = all(ocr._plausible(token, settings) is expected for token, expected in cases)
    check("number plausibility filter", ok)

    # A sponsor word must not be bent into digits, but a near-numeric token
    # with one lookalike letter should be.
    check("lookalike letters only bend near-numeric tokens",
          ocr._normalise("BOSS") == "BOSS" and ocr._normalise("4O") == "40",
          f"BOSS -> {ocr._normalise('BOSS')}, 4O -> {ocr._normalise('4O')}")


def find_test_photo(tmp: Path) -> Path | None:
    """The bus photo bundled with Ultralytics: a real vehicle, no download."""
    try:
        import ultralytics

        asset = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
        if asset.exists():
            target = tmp / "bus.jpg"
            shutil.copy(asset, target)
            return target
    except Exception:
        pass
    return None


def test_detection(tmp: Path) -> list:
    photo = find_test_photo(tmp)
    if not photo:
        check("detection", False, "no test photo available")
        return []

    settings = Settings()
    detections = detect_mod.detect(photo, settings)
    check("detector finds a vehicle", len(detections) > 0,
          f"{len(detections)} found: {[d.cls for d in detections]}")
    if not detections:
        return []

    detect_mod.write_crops(photo, detections, settings, "smoketest")
    crop = detections[0].crop_path
    check("crop written", crop is not None and crop.exists(),
          str(crop) if crop else "")
    return detections


def test_ocr(tmp: Path) -> None:
    """Render a plain number and confirm the OCR path reads it."""
    img = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arialbd.ttf", 180)
    except Exception:
        font = ImageFont.load_default()
    draw.text((110, 40), "42", fill="black", font=font)
    path = tmp / "number.jpg"
    img.save(path, quality=95)

    reading = ocr.read_number(path, Settings())
    check("OCR reads a rendered number", reading.number == "42",
          f"got {reading.number!r} at {reading.confidence:.2f}")


def test_vlm(detections: list) -> None:
    settings = Settings()
    try:
        vlm.check_available(settings)
    except vlm.VLMUnavailable as exc:
        check("Ollama vision model reachable", False, str(exc))
        return
    check("Ollama vision model reachable", True, settings.vlm_model)

    if not detections or not detections[0].crop_path:
        return
    reading = vlm.read_number(detections[0].crop_path, settings)
    # The bus has no competition number, so the right answer here is None.
    # What is being tested is that the call completes and parses.
    check("vision model returns a parsed answer", reading.source == "vlm",
          f"number={reading.number!r} confidence={reading.confidence:.2f}")


def test_xmp(tmp: Path) -> None:
    settings = Settings()
    jpeg = tmp / "frame.jpg"
    Image.new("RGB", (64, 48), "grey").save(jpeg)

    with ExifTool() as tool:
        result = write_keywords(tool, jpeg, ["88", "#88", "Broc Feeney"], settings)
        check("keywords written into JPEG", result.ok, result.message[:120])

        read_back = tool.read_tags([jpeg], ["XMP-dc:Subject", "IPTC:Keywords"])
        subjects = read_back[0].get("Subject") if read_back else None
        subjects = subjects if isinstance(subjects, list) else [subjects]
        check("keywords read back", "Broc Feeney" in (subjects or []), str(subjects))

        # Writing the same keywords twice must not duplicate them.
        write_keywords(tool, jpeg, ["88", "#88", "Broc Feeney"], settings)
        again = tool.read_tags([jpeg], ["XMP-dc:Subject"])
        again_subjects = again[0].get("Subject") if again else []
        again_subjects = (again_subjects if isinstance(again_subjects, list)
                          else [again_subjects])
        check("rewrites do not duplicate keywords",
              len(again_subjects) == len(subjects or []),
              f"{len(subjects or [])} -> {len(again_subjects)}")

        # A RAW-like file should get a sidecar rather than being modified.
        fake_raw = tmp / "frame.cr3"
        fake_raw.write_bytes(b"not really a raw file")
        sidecar = sidecar_for(fake_raw)
        check("sidecar path is IMG.xmp not IMG.cr3.xmp",
              sidecar.name == "frame.xmp", sidecar.name)


def test_store(tmp: Path) -> None:
    db = tmp / "test.db"
    conn = store.connect(db)
    try:
        job_id = store.create_job(conn, tmp, "smoke", {})
        store.add_images(conn, job_id, [tmp / "a.cr3", tmp / "b.cr3"])
        # Re-adding the same frames must not create duplicates.
        store.add_images(conn, job_id, [tmp / "a.cr3"])
        count = conn.execute("SELECT COUNT(*) FROM images WHERE job_id=?",
                             (job_id,)).fetchone()[0]
        check("store dedupes images", count == 2, f"{count} rows")

        image_id = conn.execute("SELECT id FROM images LIMIT 1").fetchone()[0]
        det_id = store.add_detection(conn, image_id, (0, 0, 10, 10), "car", 0.9, "x.jpg")
        store.set_number(conn, det_id, "88", "ocr", 0.91)
        row = conn.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone()
        check("store records a number", row["number"] == "88" and row["rejected"] == 0)
    finally:
        conn.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="conrod-smoke-") as raw_tmp:
        tmp = Path(raw_tmp)
        print("== mapping ==");      test_mapping(tmp)
        print("== numbers ==");      test_number_plausibility()
        print("== store ==");        test_store(tmp)
        print("== xmp ==");          test_xmp(tmp)
        print("== ocr ==");          test_ocr(tmp)
        print("== detection ==");    detections = test_detection(tmp)
        print("== vision model =="); test_vlm(detections)

    failed = [name for ok, name, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
