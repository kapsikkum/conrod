"""Every setup fixer reports the same way, and failures reach the screen.

Written after driving a fresh install by hand. Pressing Install on the car
grouping model did nothing at all -- no spinner, no error, the row unchanged.
The API had the answer the whole time:

    failed: setup_fix.<locals>.progress() takes 1 positional argument
            but 2 were given

`apply_fix` hands one callback to three fixers. Two of them called it with a
dict and one with two positional arguments, so the model behind the headline
feature of that release could not be installed at all, on any machine, ever
-- and because a failure hid the only element that reports anything, it
looked exactly like a dead button.

Two rules, both cheap, either of which would have caught it.
"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import conrod
from conrod import setup_check
from conrod.config import Settings


class EveryFixerSpeaksTheSameLanguage(unittest.TestCase):
    """One caller, one convention. `apply_fix` cannot know which fixer it
    dispatched to, so a fixer with its own signature is a crash waiting for
    somebody to press the button."""

    FIXERS = ("pull_model", "download_weights", "download_grouping_model")

    def _calls(self, name: str) -> list:
        """What this fixer passes to on_progress, with the work stubbed."""
        seen: list = []
        fixer = getattr(setup_check, name)
        with patch.object(setup_check, "_fix_lock"), \
             patch.object(setup_check, "similarity", create=True) as sim, \
             patch.object(setup_check, "httpx", create=True):
            sim.download.side_effect = lambda tick=None, *a, **k: (
                tick(1_000_000, 24_000_000) if tick else None) or True
            sim.is_ready.return_value = True
            try:
                fixer(Settings(), seen.append)
            except Exception:
                pass
        return seen

    def test_the_grouping_model_reports_like_the_others(self) -> None:
        """The one that was wrong. It called on_progress(text, share)."""
        source = inspect.getsource(setup_check.download_grouping_model)
        for call in re.findall(r"on_progress\(([^\n]*)", source):
            self.assertTrue(call.lstrip().startswith("{"),
                            f"not a dict: on_progress({call}")

    def test_no_fixer_takes_two_positional_arguments(self) -> None:
        for name in self.FIXERS:
            source = inspect.getsource(getattr(setup_check, name))
            for call in re.findall(r"on_progress\(([^\n]*)", source):
                self.assertTrue(call.lstrip().startswith("{"),
                                f"{name} calls on_progress({call}")

    def test_the_dispatcher_hands_all_of_them_one_callback(self) -> None:
        """Which is what makes the rule above load-bearing rather than a
        style preference."""
        source = inspect.getsource(setup_check.apply_fix)
        for name in self.FIXERS:
            self.assertIn(f"{name}(settings, on_progress)", source)

    def test_the_server_callback_takes_exactly_one_argument(self) -> None:
        server = (Path(conrod.__file__).parent / "server.py").read_text(
            encoding="utf-8")
        body = server[server.index("def setup_fix("):]
        self.assertIn("def progress(event: dict) -> None:", body[:600])


class AFailureReachesTheScreen(unittest.TestCase):
    """The reason was in `_fix["status"]` and the page never showed it.

    Worse than never showing it: the element that reports was hidden
    whenever nothing was running, and a failure sets active to false. So it
    appeared for 900ms and then the list re-rendered over the top.
    """

    def setUp(self) -> None:
        self.js = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_the_status_is_rendered_not_only_the_percentage(self) -> None:
        body = self.js[self.js.index("async function pollFix("):]
        body = body[:body.index("\n}")]
        self.assertIn("fix.status", body)

    def test_a_failure_is_not_cleared_away(self) -> None:
        body = self.js[self.js.index("async function pollFix("):]
        self.assertIn('startsWith("failed")', body[:1600])

    def test_the_progress_sits_in_the_row_being_installed(self) -> None:
        """It used to be one shared strip at the foot of a seven-row list,
        a screen away from the button that had been pressed."""
        self.assertIn('row.dataset.fix = check.fix', self.js)
        self.assertIn('[data-fix="${name}"]', self.js)

    def test_the_old_shared_strip_is_gone(self) -> None:
        page = (Path(conrod.__file__).parent / "web" / "index.html").read_text(
            encoding="utf-8")
        self.assertNotIn('id="fix-progress"', page)
        self.assertNotIn('fix-fill', self.js)


if __name__ == "__main__":
    unittest.main()
