"""The review grid's own CSS used to declare `.card` in six separate,
non-adjacent places -- base rules, focus/rating colours, chips, rated-border
colours, keyboard-cursor outline and action-button sizing -- added
piecemeal as each feature (culling, grouping, stars, keyboard nav) landed,
with no shared component to extend. `.tag`, `.fact` and `.grouptag` each
reinvented their own padding, radius and border for what is visually the
same small pill -- `.grouptag` itself is gone now, its one job (saying how
much of a group agreed) folded into the header's own `.fact.count`.

Nothing here checks how it looks -- that was done by eye, rendering the
real stylesheet against sample markup -- only that the two structural
complaints stay fixed: one `.card` declaration, and one shared pill base
that colour-only modifiers build on.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import conrod


def _rule_starts(css: str, selector_prefix: str) -> list[str]:
    """Top-level selectors (not inside another rule) starting with the
    given text, e.g. every place ".card {" or ".card." begins a rule."""
    # Comments are stripped first so an old rule mentioned in a comment
    # (as this file's own docstring-in-CSS above does) is never counted.
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [m.group(0) for m in re.finditer(
        rf"^{re.escape(selector_prefix)}[^{{]*\{{", stripped, flags=re.M)]


class OneCardDeclaration(unittest.TestCase):
    def setUp(self):
        self.css = (Path(conrod.__file__).parent / "web" / "style.css").read_text(
            encoding="utf-8")

    def test_the_bare_card_selector_is_declared_exactly_once(self) -> None:
        bare = [line for line in _rule_starts(self.css, ".card")
               if re.match(r"^\.card\s*\{", line)]
        self.assertEqual(len(bare), 1,
                         f"expected one `.card {{` rule, found {len(bare)}")

    def test_every_card_rule_lives_in_the_same_stretch_of_the_file(self) -> None:
        """Not just one base rule -- every rule that touches a card, a
        vehicle header, or the shared pill family, in one contiguous
        section rather than scattered through the file."""
        markers = ("\n.card ", "\n.card.", "\n.card{", ".vehicle-head {",
                  ".tag, .fact {")
        positions = [self.css.index(m) for m in markers if m in self.css]
        self.assertTrue(positions, "none of the expected card rules were found")
        span = max(positions) - min(positions)
        # The whole file is under 30,000 characters; the card section
        # should be a stretch of it, not spread across most of the file.
        self.assertLess(span, 12000,
                        "card-related rules are spread too far apart again")

    def test_tag_and_fact_share_one_pill_base(self) -> None:
        self.assertIn(".tag, .fact {", self.css)

    def test_grouptag_is_actually_gone_not_just_unused(self) -> None:
        """Its one job -- how much of a group agreed -- is said once on the
        header now (`.fact.count`), not per card, so nothing should still
        define or reach for a `.grouptag` rule."""
        self.assertNotIn("grouptag", self.css)


if __name__ == "__main__":
    unittest.main()
