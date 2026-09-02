"""Grouping and consensus.

These encode what eight frames of one blue Ford Falcon actually produced when
qwen2.5vl:7b was asked to name it: three Fairmonts, two Mustangs, a Fiesta, a
"Vauxhall Astra" and a Commodore. Grouping exists because that spread is
normal, so the tests use the real spread rather than a tidy invented one.
"""

from __future__ import annotations

import unittest

from conrod.grouping import consensus

# What the model returned for the eight Falcon frames, verbatim.
FALCON = (
    [{"make": "Ford", "model": "Fairmont", "colour": "blue"}] * 3
    + [{"make": "Ford", "model": "Mustang", "colour": "blue"}] * 2
    + [
        {"make": "Ford", "model": "Fiesta", "colour": "blue"},
        {"make": "Holden", "model": "Vauxhall Astra", "colour": "blue"},
        {"make": "Holden", "model": "Holden Commodore", "colour": "blue"},
    ]
)


class Consensus(unittest.TestCase):
    def test_a_disputed_group_keeps_the_make_the_majority_agreed_on(self):
        # No model name has a majority, but six of eight said Ford.
        out = consensus(FALCON)
        self.assertEqual(out.make, "Ford")
        self.assertIsNone(out.model)
        self.assertAlmostEqual(out.agreement, 0.75)

    def test_the_disagreement_is_still_reported(self):
        out = consensus(FALCON)
        self.assertIn("Ford Fairmont", out.disputed)
        self.assertIn("Holden Vauxhall Astra", out.disputed)

    def test_colour_survives_when_the_name_does_not(self):
        self.assertEqual(consensus(FALCON).colour, "blue")

    def test_no_make_majority_means_no_name_at_all(self):
        # Two Holdens out of four is not enough to write "Holden" down.
        out = consensus([
            {"make": "Ford", "model": "Fiesta"},
            {"make": "Holden", "model": "Astra"},
            {"make": "Holden", "model": "Commodore"},
            {"make": "Toyota", "model": "Corolla"},
        ])
        self.assertIsNone(out.make)
        self.assertIsNone(out.model)

    def test_make_and_model_are_never_voted_apart(self):
        # The original bug: voting the fields separately produced "Holden
        # Fiesta" -- a name no frame gave and no car has.
        out = consensus([
            {"make": "Ford", "model": "Fiesta"},
            {"make": "Holden", "model": "Astra"},
            {"make": "Holden", "model": "Commodore"},
        ])
        self.assertNotEqual((out.make, out.model), ("Holden", "Fiesta"))

    def test_an_agreeing_group_keeps_the_full_name(self):
        out = consensus([{"make": "Mitsubishi", "model": "Outlander",
                          "colour": "grey"}] * 3)
        self.assertEqual((out.make, out.model), ("Mitsubishi", "Outlander"))
        self.assertEqual(out.agreement, 1.0)
        self.assertEqual(out.disputed, [])

    def test_plates_are_taken_from_the_most_confident_read_not_voted(self):
        out = consensus([
            {"make": "Ford", "model": "Falcon", "plate": "AAA11A", "plate_conf": 0.4},
            {"make": "Ford", "model": "Falcon", "plate": "AAA11A", "plate_conf": 0.4},
            {"make": "Ford", "model": "Falcon", "plate": "73111J", "plate_conf": 0.91},
        ])
        self.assertEqual(out.plate, "73111J")


if __name__ == "__main__":
    unittest.main()
