"""When a card is allowed to say "Independent".

Found on the photographer's own album. Every card Conrod had managed to
identify opened with the same word:

    Independent · Gray Mitsu…
    Independent · Blue Ford …
    Independent · Blue Volks…
    Independent · Blue Merce…

Two faults at once, and they compound.

The test for it was `identified(attrs)` -- does this crop have *anything*
read off it -- and the vision model returns a colour for very nearly
everything. So on that album the word went on 115 of the 119 cars it had
read, of which 13 were competition cars at all. It is a statement about a
competition entry: this car is running, and it is running for nobody. Said
about a road car at a Sunday meet it is not wrong so much as meaningless.

And it led the title. The team slot comes first because a team is how a
race car is known -- but "Independent" is the *absence* of a team, so
leading with it put the same word at the front of every card and pushed
the actual car off the end of the available width. The half that got cut
was the useful half.

No browser here: the two functions are pulled out of app.js and run under
node, the way the box-label tests do it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import conrod

APP_JS = Path(conrod.__file__).parent / "web" / "app.js"


def _run(expression: str, attrs: dict) -> str:
    """Evaluate one expression against the real functions from app.js."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function identified(")
    end = source.index("\n/* ", start)
    driver = (source[start:end]
              + f"\nconst attrs = {json.dumps(attrs)};"
              + f"\nconsole.log(String({expression}));")
    out = subprocess.run([shutil.which("node"), "-e", driver],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode:                              # pragma: no cover
        raise AssertionError(out.stderr)
    return out.stdout.strip()


class OnlyForACompetitionCar(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def test_a_road_car_with_a_colour_is_not_independent(self) -> None:
        """The case that put the word on 115 cars. A blue hatchback at a
        cruise is independent of nothing."""
        self.assertEqual(
            _run("privateer(attrs)",
                 {"colour": "blue", "make": "Ford", "is_competition": False}),
            "false")

    def test_a_competition_car_with_no_team_is(self) -> None:
        self.assertEqual(
            _run("privateer(attrs)",
                 {"colour": "blue", "is_competition": True, "team": None}),
            "true")

    def test_a_competition_car_with_a_team_is_not(self) -> None:
        """It has one. The word is for the absence."""
        self.assertEqual(
            _run("privateer(attrs)",
                 {"is_competition": True, "team": "CV Performance"}),
            "false")

    def test_a_frame_nothing_was_read_off_is_not(self) -> None:
        self.assertEqual(_run("privateer(attrs)", {}), "false")

    def test_it_is_not_the_same_question_as_being_identified(self) -> None:
        """The old code used one for the other, which is the whole bug."""
        attrs = {"colour": "blue", "is_competition": False}
        self.assertEqual(_run("identified(attrs)", attrs), "true")
        self.assertEqual(_run("privateer(attrs)", attrs), "false")


class WhereItSitsInTheTitle(unittest.TestCase):
    """A qualifier, not an identity. It goes after the car."""

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def test_the_word_no_longer_leads_the_title(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        block = source[source.index("const bits = [];"):]
        block = block[:block.index("return bits.join")]
        name = block.index("bits.push(name)")
        independent = block.index("independent")
        self.assertGreater(independent, name,
                           "the car has to be named before it is qualified")

    def test_a_real_team_still_leads(self) -> None:
        """A team is how a race car is known, so that one keeps its place."""
        source = APP_JS.read_text(encoding="utf-8")
        block = source[source.index("const bits = [];"):]
        block = block[:block.index("return bits.join")]
        self.assertLess(block.index("bits.push(attrs.team)"),
                        block.index("bits.push(name)"))


if __name__ == "__main__":
    unittest.main()
