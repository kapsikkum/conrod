"""Whether the subject is actually inside the photograph.

Focus cannot answer this. A car cut in half by the frame edge is frequently
pin sharp, which is why the two are measured apart and only combined at the
end.
"""

from __future__ import annotations

import unittest

from conrod import framing, sharpness

FRAME = (4000, 3000)


class Clipping(unittest.TestCase):
    def test_a_subject_with_air_around_it_is_not_clipped(self) -> None:
        out = framing.assess((800, 600, 3200, 2400), *FRAME)
        self.assertEqual(out.sides, 0)
        self.assertEqual(out.factor, 1.0)
        self.assertFalse(out.cut_off)

    def test_running_off_one_edge(self) -> None:
        out = framing.assess((2000, 600, 4000, 2400), *FRAME)
        self.assertEqual(out.sides, 1)
        self.assertLess(out.factor, 1.0)
        # One edge is a blemish, not a write-off.
        self.assertFalse(out.cut_off)

    def test_a_corner_takes_two_edges(self) -> None:
        out = framing.assess((0, 0, 1200, 900), *FRAME)
        self.assertEqual(out.sides, 2)
        self.assertTrue(out.cut_off)

    def test_two_edges_cost_more_than_twice_one(self) -> None:
        """A car missing its wheels and its nose is not twice as flawed.

        It is usually unusable, and the penalty has to grow accordingly.
        """
        one = framing.assess((2000, 600, 4000, 2400), *FRAME)
        two = framing.assess((0, 0, 1200, 900), *FRAME)
        self.assertLess(two.factor, one.factor)
        self.assertLess(1.0 - two.factor, 2 * (1.0 - one.factor) + 0.001)

    def test_a_few_pixels_short_still_counts(self) -> None:
        """Detector boxes rarely land exactly on the boundary."""
        self.assertEqual(framing.assess((3, 600, 3200, 2400), *FRAME).sides, 1)

    def test_nonsense_input_is_not_an_error(self) -> None:
        self.assertEqual(framing.assess(None, *FRAME).sides, 0)
        self.assertEqual(framing.assess((0, 0, 10, 10), 0, 0).sides, 0)

    def test_it_says_what_it_found(self) -> None:
        self.assertEqual(framing.describe(framing.Framing(sides=0)), "")
        self.assertIn("edge", framing.describe(framing.Framing(sides=1)))
        self.assertIn("2", framing.describe(framing.Framing(sides=2)))


class Rating(unittest.TestCase):
    """Clipping has to actually lower the number the cull looks at."""

    def test_being_cut_off_lowers_the_rating(self) -> None:
        focus = 0.80
        clear = focus * framing.assess((800, 600, 3200, 2400), *FRAME).factor
        cut = focus * framing.assess((0, 0, 1200, 900), *FRAME).factor
        self.assertLess(cut, clear)

    def test_a_sharp_frame_cut_in_half_can_fall_out_of_good(self) -> None:
        """The case the whole thing exists for: sharp, and half missing."""
        focus = 0.62
        cut = focus * framing.assess((0, 0, 1200, 900), *FRAME).factor
        self.assertEqual(sharpness.rating_for(focus, 0.52, 0.25), "good")
        self.assertNotEqual(sharpness.rating_for(cut, 0.52, 0.25), "good")

    def test_the_rating_bands_are_named_for_pictures(self) -> None:
        """"Blurred" would be a lie about a sharp frame that ran off the edge."""
        self.assertEqual(sharpness.rating_for(0.90, 0.52, 0.25), "good")
        self.assertEqual(sharpness.rating_for(0.40, 0.52, 0.25), "fair")
        self.assertEqual(sharpness.rating_for(0.10, 0.52, 0.25), "poor")


if __name__ == "__main__":
    unittest.main()
