"""What the live view writes on a box drawn over a frame.

Two faults met on one screenshot: a Mercedes, correctly detected, labelled
"car . 0%". Neither half was true of the car.

The 0% was a literal in the identify stage's progress event -- the
detector's real score was in the row all along and was not read back.
Nobody could tell it from a genuine score, so it read as the program
saying it did not believe in a detection it had drawn a box around.

The "car" was the label never once looking at the identification. The
server had been attaching the make and model to every box as they came
back, and the only two things the label considered were the race number
and the detector's class -- so an identify pass drew "car" over every
vehicle it had just named, and the frames where the vision model had
actually failed looked exactly the same.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import conrod

APP_JS = Path(conrod.__file__).parent / "web" / "app.js"


def _label(box: dict) -> str:
    """Run the real boxLabel from app.js over one box."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function boxLabel(")
    end = source.index("\nfunction ", start + 1)
    driver = source[start:end] + f"\nconsole.log(boxLabel({json.dumps(box)}));"
    # utf-8 explicitly: the separator in these labels is a middle dot, and
    # Windows hands back cp1252 by default, which turns it into a mojibake
    # question mark and fails every assertion for the wrong reason.
    out = subprocess.run([shutil.which("node"), "-e", driver],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode:                              # pragma: no cover
        raise AssertionError(out.stderr)
    return out.stdout.strip()


class TheLabelOnABox(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def test_a_race_number_wins(self) -> None:
        """It is the identity at a motorsport meet, and it is the thing
        the photographer is looking for as the frames go past."""
        self.assertEqual(_label({"number": "62", "read_conf": 0.91,
                                 "title": "Blue Holden Commodore", "kind": "car"}),
                         "#62 · 91%")

    def test_otherwise_what_the_car_turned_out_to_be(self) -> None:
        self.assertEqual(_label({"title": "Green Ford Mustang", "kind": "car",
                                 "conf": 0.86}),
                         "Green Ford Mustang")

    def test_and_only_then_what_the_detector_saw(self) -> None:
        self.assertEqual(_label({"kind": "car", "conf": 0.86}), "Car · 86%")

    def test_the_detector_class_is_not_shown_in_lowercase(self) -> None:
        """A bare "car" reads as something the program failed to fill in,
        because that is usually what it was."""
        self.assertEqual(_label({"kind": "motorcycle", "conf": 0.5}),
                         "Motorcycle · 50%")

    def test_no_confidence_is_shown_rather_than_zero(self) -> None:
        """A missing score and a score of zero are not the same claim, and
        only one of them has ever actually happened."""
        self.assertEqual(_label({"kind": "car", "conf": 0}), "Car")
        self.assertEqual(_label({"kind": "car"}), "Car")

    def test_a_box_with_nothing_on_it_still_says_something(self) -> None:
        self.assertEqual(_label({}), "Vehicle")


class TheIdentifyStageReportsTheRealScore(unittest.TestCase):
    def test_the_confidence_is_read_back_not_invented(self) -> None:
        import inspect

        from conrod import pipeline

        source = inspect.getsource(pipeline._announce_frame)
        self.assertIn("conf", inspect.signature(pipeline._announce_frame).parameters)
        self.assertNotIn('"conf": 0.0', source)

    def test_the_query_feeding_it_selects_that_column(self) -> None:
        """It did not, which is why there was nothing to pass."""
        import inspect

        from conrod import pipeline

        self.assertIn("d.conf", inspect.getsource(pipeline.identify))


if __name__ == "__main__":
    unittest.main()
