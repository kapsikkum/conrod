"""One reader crashing must not blank the other three.

Found on a real album: sixteen consecutive detections came back with
absolutely nothing -- no plate, but also no make, no colour, no team, on
frames the vision model would otherwise have read fine. The only net
around the four readers was `_analysis_worker`'s own catch-all, which
discarded the whole `VehicleAnalysis` the moment any single one of them
raised, and there was no record anywhere of what had actually failed.

`analyze()` now gives each reader its own try/except, so a crash in one
costs only that reader's fields, and writes what happened to conrod.log
instead of vanishing into an ephemeral progress message nobody was
necessarily watching at the time.

Everything here runs offline: all four readers are mocked in every test,
so a crash on the one under test is never confused with rapidocr or Ollama
simply not being installed in the environment running the suite.
"""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from conrod import ocr, plates, vlm
from conrod.analyze import analyze
from conrod.config import Settings


def _blank_crop() -> Image.Image:
    return Image.new("RGB", (64, 64), "grey")


@contextlib.contextmanager
def _readers(*, plate=None, number=None, described=None, text=None):
    """Stub all four readers at once; pass only the one(s) a test cares about."""
    plate = plate or (lambda *a, **k: (plates.PlateReading(), []))
    number = number or (lambda *a, **k: ocr.Reading(None, 0.0))
    described = described or (lambda *a, **k: vlm.VehicleDescription())
    text = text or (lambda *a, **k: [])
    with patch.object(plates, "scan_regions", side_effect=plate), \
         patch.object(ocr, "read_number", side_effect=number), \
         patch.object(vlm, "describe", side_effect=described), \
         patch.object(ocr, "visible_text", side_effect=text):
        yield


class AReaderCrashing(unittest.TestCase):
    def test_a_plate_crash_does_not_lose_the_vehicle_description(self) -> None:
        with _readers(
            plate=RuntimeError("boom"),
            described=lambda *a, **k: vlm.VehicleDescription(
                make="Mini", colour="green", confidence=0.9),
        ):
            result = analyze(_blank_crop(), Settings())
        self.assertIsNone(result.plate)
        self.assertEqual(result.make, "Mini")
        self.assertEqual(result.colour, "green")

    def test_a_vlm_crash_does_not_lose_the_plate(self) -> None:
        reading = plates.PlateReading(text="39432J", confidence=0.9)
        with _readers(plate=lambda *a, **k: (reading, []), described=RuntimeError("boom")):
            result = analyze(_blank_crop(), Settings())
        self.assertEqual(result.plate, "39432J")
        self.assertIsNone(result.make)

    def test_a_text_read_crash_does_not_lose_the_number(self) -> None:
        with _readers(number=lambda *a, **k: ocr.Reading("21", 0.9, "ocr"),
                     text=RuntimeError("boom")):
            result = analyze(_blank_crop(), Settings())
        self.assertEqual(result.race_number, "21")
        self.assertEqual(result.text, [])

    def test_a_number_read_crash_does_not_lose_the_plate(self) -> None:
        reading = plates.PlateReading(text="39432J", confidence=0.9)
        with _readers(plate=lambda *a, **k: (reading, []), number=RuntimeError("boom")):
            result = analyze(_blank_crop(), Settings())
        self.assertEqual(result.plate, "39432J")
        self.assertIsNone(result.race_number)

    def test_a_crashing_reader_writes_down_what_happened(self) -> None:
        """Found by hand-replaying the exact stored crop and getting a good
        read where the live pipeline had stored nothing -- with no way to
        tell, after the fact, which of the four readers had actually failed
        or why. The failure has to end up somewhere durable, not only in the
        progress stream."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "conrod.log"
            with patch("conrod.config.LOG_PATH", log_path), \
                 _readers(plate=RuntimeError("boom")):
                analyze(_blank_crop(), Settings())
            self.assertTrue(log_path.exists())
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("plate read failed", text)
            self.assertIn("boom", text)


if __name__ == "__main__":
    unittest.main()
