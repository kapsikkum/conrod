"""Two things found together while looking at the review grid by eye:

A vehicle with no team read showed nothing at all where the team would go
-- indistinguishable from "this has not been identified yet." A privateer
is a common, real answer at a car meet, not a gap, so it says so.

That label needed a working "has this been identified" check to gate on,
and the existing one had just gone silently wrong: item.attributes is now
always a full object (every field present, most null) rather than {} until
identify() runs, so the old `Object.keys(attrs).length` test -- which used
to mean "this frame has something" -- started meaning "this is an object,"
which is always true. `identified()` checks for actual content instead.

The same check decides what goes in the Unknown stack: a frame nothing has
been read off is not a vehicle of its own, it is a leftover to be corrected
into one.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import conrod


class IndependentIsARealAnswer(unittest.TestCase):
    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_a_stack_names_a_privateer_rather_than_leaving_a_gap(self) -> None:
        name = self.code[self.code.index("function stackName("):]
        self.assertIn('"Independent"', name[:900])

    def test_a_gallery_card_says_it_too(self) -> None:
        """The stack name is a summary; inside the gallery each frame still
        states the team it was read with, so a mis-grouped frame is visible
        against the ones around it. Said beside the empty team chips rather
        than as the value, because "Independent" is not a name anyone typed
        -- it is what an empty team field means."""
        card = self.code[self.code.index("function galleryCard("):]
        self.assertIn('"Independent"', card[:6000])
        self.assertIn("muted-note", card[:6000])

    def test_independent_only_shows_once_something_has_actually_been_read(self) -> None:
        """Not claimed for a frame still waiting on identify() -- that would
        say "checked, no team" about a frame nothing has looked at."""
        self.assertIn("identified(attrs)", self.code)

    def test_has_content_replaces_has_any_keys(self) -> None:
        """The object-keys check stopped meaning anything the moment the
        server started always sending a full attributes object -- it would
        have been true for every card, identified or not."""
        self.assertNotIn("Object.keys(m.attributes", self.code)
        self.assertIn("function identified(attrs)", self.code)


class TheUnknownStack(unittest.TestCase):
    """A scan that has not finished identifying used to produce a screen of
    hundreds of one-frame "vehicles" with empty headers, each claiming to be
    a distinct car. They are one pile now -- somewhere to go and correct
    them from, rather than noise to scroll past."""

    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_unread_frames_all_land_in_one_stack(self) -> None:
        grouping = self.code[self.code.index("function groupItems("):]
        self.assertIn("const named = identified(item.attributes)", grouping[:600])
        self.assertIn("UNKNOWN", grouping[:600])

    def test_a_frame_with_only_a_number_or_plate_is_not_unknown(self) -> None:
        """The vision model is not the only reader. A frame with a plate off
        the OCR is identified enough to stand as its own vehicle."""
        grouping = self.code[self.code.index("function groupItems("):]
        self.assertIn("|| item.number || item.plate", grouping[:600])

    def test_the_leftovers_sort_last(self) -> None:
        grouping = self.code[self.code.index("function groupItems("):]
        self.assertIn("(a === UNKNOWN) - (b === UNKNOWN)", grouping[:900])


if __name__ == "__main__":
    unittest.main()
