"""The card's title used to fall back to the bare, lowercase detector class
-- "car" -- the moment make/model/colour were all empty, which happened far
more than intended: any detection not yet identified (mid-scan) had no
`kind` of its own stored yet, so it inherited the dataclass default of
"car" regardless of whether the detector had actually found a motorcycle.
A crashed reader (fixed separately, see test_analyze_resilience.py) produced
the same bare "car" the same way.

Two things had to change: the fallback text itself, so a generic
classification never looks identical to "nothing has been read yet"; and
where the classification comes from, so it is always the detector's own
answer rather than a stale or unset one.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from conrod.analyze import VehicleAnalysis


class TheFallbackTitle(unittest.TestCase):
    def test_a_real_identification_is_unaffected(self) -> None:
        analysis = VehicleAnalysis(make="Mini", model="Cooper S", colour="green")
        self.assertEqual(analysis.title, "Green Mini Cooper S")

    def test_nothing_read_at_all_says_so_plainly(self) -> None:
        """The exact bug: a black Mini with an empty analysis showed "car",
        indistinguishable from a real (if generic) classification."""
        analysis = VehicleAnalysis(kind="car")
        self.assertEqual(analysis.title, "Car, not identified")

    def test_a_bike_not_yet_identified_says_bike_not_car(self) -> None:
        analysis = VehicleAnalysis(kind="motorcycle")
        self.assertEqual(analysis.title, "Motorcycle, not identified")

    def test_a_plate_or_number_without_a_name_is_not_call_unidentified(self) -> None:
        """Something does identify this vehicle to the photographer even
        though nothing named the make or model -- the plate or number will
        show elsewhere on the card, so saying "not identified" next to it
        would contradict what is right beside it."""
        with_plate = VehicleAnalysis(kind="car", plate="39432J")
        self.assertEqual(with_plate.title, "Car")
        with_number = VehicleAnalysis(kind="car", race_number="21")
        self.assertEqual(with_number.title, "#21 Car")

    def test_the_race_number_still_prefixes_every_case(self) -> None:
        self.assertEqual(
            VehicleAnalysis(kind="car", race_number="21", make="Mini").title,
            "#21 Mini")


class TheKindComesFromTheDetectorNotTheAnalysis(unittest.TestCase):
    """`VehicleAnalysis.kind` only reflects reality once identify() has
    actually run for that detection -- before that it is the dataclass
    default. The detection's own `cls` column is ground truth from the
    moment the box exists, so server.py has to prefer it. Checked from the
    source rather than through a live endpoint: both call sites read from
    the shared, in-use ~/.conrod database, which a test must never write
    to while a real scan could be running against it."""

    def setUp(self):
        import conrod

        self.code = (Path(conrod.__file__) / "..").resolve()
        self.source = (self.code / "server.py").read_text(encoding="utf-8")

    def _body(self, name: str) -> str:
        """One function's source, ending where the next one starts.

        Bounded by the next `def` at module level rather than by a character
        count. A fixed slice made this fail the moment anything was added
        above the line it checks -- the assertion was right and the window
        was arbitrary.
        """
        start = self.source.index(f"def {name}(")
        rest = self.source[start:]
        end = re.search(r"\n(?:@app\.|def |class )", rest[1:])
        return rest[:end.start() + 1] if end else rest

    def test_the_listing_endpoint_prefers_the_detections_own_class(self) -> None:
        self.assertIn('analysis.kind = item["cls"]', self._body("detections"))

    def test_the_update_endpoint_prefers_the_detections_own_class(self) -> None:
        self.assertIn('analysis.kind = row["cls"]', self._body("update_detection"))


if __name__ == "__main__":
    unittest.main()
