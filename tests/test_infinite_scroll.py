"""Going through a shoot used to be: scroll, reach for the mouse, click
"Load more", scroll again -- once per hundred and twenty frames, for two
thousand frames.

Driven by the scroll event rather than an IntersectionObserver. The
observer is the tidier mechanism, but it did not fire at all in one of the
surfaces this gets looked at in, and neither did requestAnimationFrame --
both are tied to painting. A feed that silently stops feeding is worse
than a plain listener.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import conrod


class TheFeed(unittest.TestCase):
    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        self.html = (Path(conrod.__file__).parent / "web" / "index.html").read_text(
            encoding="utf-8")

    def test_there_is_no_load_more_button_left(self) -> None:
        self.assertNotIn("Load more", self.html)
        self.assertIn('id="more" class="more-sentinel"', self.html)

    def test_scrolling_is_what_asks_for_the_next_page(self) -> None:
        self.assertIn('$("#grid-wrap").addEventListener("scroll"', self.code)

    def test_it_does_not_depend_on_painting(self) -> None:
        """rAF and IntersectionObserver both go quiet in a pane that is not
        painting, which is exactly where this was first tried. Checked as
        calls, not mentions -- both are named in comments saying why they
        are not what drives this."""
        import re

        code = re.sub(r"//[^\n]*", "", self.code)          # line comments
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)  # block comments
        self.assertNotIn("requestAnimationFrame(", code)
        self.assertNotIn("new IntersectionObserver(", code)

    def test_it_keeps_going_until_everything_is_loaded(self) -> None:
        """The grid collapses thousands of frames into a handful of stacks,
        so a page that cannot scroll is normal rather than a reason to stop.
        Capping it left 840 of 1,667 loaded and no way to reach the rest --
        the count that would have released it only reset on a scroll that
        could never happen."""
        feed = self.code[self.code.index("function maybeLoadMore()"):]
        body = feed[:feed.index("function renderFoot()")]
        self.assertNotIn("AUTOFILL", body)
        self.assertIn("state.items.length >= state.total", body)

    def test_it_only_holds_off_while_there_is_page_left(self) -> None:
        feed = self.code[self.code.index("function maybeLoadMore()"):]
        self.assertIn("room > 0 && pane.scrollTop < room - 600", feed[:900])

    def test_the_foot_counts_up_to_the_total(self) -> None:
        foot = self.code[self.code.index("function renderFoot()"):]
        self.assertIn("of ", foot[:600])
        self.assertIn("seen >= state.total", foot[:600])

    def test_two_pages_are_never_in_flight_at_once(self) -> None:
        feed = self.code[self.code.index("function maybeLoadMore()"):]
        self.assertIn("state.loadingMore", feed[:1600])

    def test_it_stops_at_the_end(self) -> None:
        feed = self.code[self.code.index("function maybeLoadMore()"):]
        self.assertIn("state.items.length >= state.total", feed[:1600])


class TheStackCaption(unittest.TestCase):
    def test_the_competition_number_is_not_printed_twice(self) -> None:
        """VehicleAnalysis.title already puts "#21" on the front, so a
        caption that prepends the number as well read
        "#21 · Nosse · #21 Black Mini Cooper"."""
        code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        name = code[code.index("function stackName(key, members)"):]
        self.assertIn('replace(/^#\S+\s+/, "")', name[:1200])


if __name__ == "__main__":
    unittest.main()
