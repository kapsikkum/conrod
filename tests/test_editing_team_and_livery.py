"""The vision model reads a team and a livery off the car, and is often
close without being right -- "Nosse" for "Nosso", the model name picked up
off a badge and filed as a sponsor. Until now there was nowhere to correct
either: both were plain text printed on the card, so a wrong one stayed
wrong all the way into the XMP.

They are edited as chips now, the way an email client edits recipients: a
value is a rectangle with an x, and a + opens a box for the next one. One
team, because a vehicle has one; as many sponsors as are on the car.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import conrod
from conrod import server
from conrod.analyze import VehicleAnalysis


class TheChipEditor(unittest.TestCase):
    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_both_fields_are_editable(self) -> None:
        card = self.code[self.code.index("function galleryCard("):]
        self.assertIn("chipField({", card)
        self.assertIn('placeholder: "team or entrant"', card)
        self.assertIn('placeholder: "sponsor"', card)

    def test_a_vehicle_gets_one_team_and_many_sponsors(self) -> None:
        card = self.code[self.code.index("function galleryCard("):]
        team = card[card.index('values: attrs.team'):]
        self.assertIn("single: true", team[:200])
        livery = card[card.index("values: (attrs.sponsors"):]
        self.assertNotIn("single: true", livery[:200])

    def test_a_chip_can_be_taken_off_again(self) -> None:
        """Adding without removing is half an editor -- the common correction
        is a sponsor that was never on the car."""
        fn = self.code[self.code.index("function chipField("):]
        self.assertIn('className: "chip-x"', fn[:2500])

    def test_escape_abandons_what_was_typed(self) -> None:
        fn = self.code[self.code.index("function chipField("):]
        self.assertIn('ev.key === "Escape"', fn)


class TheServerTakesTheList(unittest.TestCase):
    def test_sponsors_are_accepted_as_a_list(self) -> None:
        source = Path(server.__file__).read_text(encoding="utf-8")
        body = source[source.index("def update_detection"):]
        self.assertIn('if "sponsors" in body.attributes:', body[:3000])

    def test_blanks_and_repeats_are_dropped(self) -> None:
        """The same name typed twice, or with different spacing, would
        otherwise reach the catalogue as two keywords."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        body = source[source.index("def update_detection"):]
        block = body[body.index('if "sponsors" in body.attributes:'):][:700]
        self.assertIn("text.upper() not in seen", block)

    def test_a_typed_team_no_longer_needs_corroborating(self) -> None:
        """The warning is for a name the model invented, not one a person
        put there."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        body = source[source.index("def update_detection"):]
        self.assertIn("analysis.team_corroborated = True", body[:3000])

    def test_sponsors_survive_the_round_trip(self) -> None:
        analysis = VehicleAnalysis(sponsors=["Betta", "CV Performance"])
        self.assertEqual(
            VehicleAnalysis.from_json(analysis.to_json()).sponsors,
            ["Betta", "CV Performance"])


if __name__ == "__main__":
    unittest.main()
