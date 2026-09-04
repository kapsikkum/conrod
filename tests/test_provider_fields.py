"""Four providers want different things, and the settings screen used to
ask for all of it at once: an Ollama address and an API key, side by side,
whichever one was selected. Asking where Ollama is listening while the
provider is set to Gemini is a question with no right answer.

The fields follow the choice now, and so do their hints -- "the name
OpenAI expects, e.g. gpt-4o" rather than one line listing all four.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import conrod
from conrod.config import Settings


class TheFieldsFollowTheProvider(unittest.TestCase):
    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_the_ollama_address_is_only_asked_for_when_using_ollama(self) -> None:
        self.assertIn('(s) => s.vlm_provider === "ollama"', self.code)

    def test_the_api_key_is_only_asked_for_when_it_is_needed(self) -> None:
        self.assertIn('s.vlm_provider && s.vlm_provider !== "ollama"', self.code)

    def test_changing_the_provider_re_evaluates_the_rows(self) -> None:
        """A select that changes what the other rows mean has to redraw
        them; otherwise the right fields only appear on a reload."""
        self.assertIn("settingsCache[key] = input.value; refresh();", self.code)

    def test_every_provider_has_a_name_and_an_example_model(self) -> None:
        names = self.code[self.code.index("const PROVIDER_NAMES = {"):]
        examples = self.code[self.code.index("const MODEL_EXAMPLES = {"):]
        for provider in ("ollama", "openai", "anthropic", "gemini"):
            self.assertIn(provider, names[:300])
            self.assertIn(provider, examples[:400])

    def test_the_options_match_what_the_backend_accepts(self) -> None:
        """A provider offered in the dropdown that the server cannot
        dispatch to would fail on the first crop of a scan."""
        from conrod import vlm_providers

        row = self.code[self.code.index('["vlm_provider", "Provider", "select",'):]
        offered = row[row.index("[", row.index("select")) + 1:row.index("]")]
        offered = {p.strip().strip('"') for p in offered.split(",")}
        self.assertEqual(offered, set(vlm_providers._PROVIDERS))

    def test_the_default_is_the_one_that_needs_no_key(self) -> None:
        self.assertEqual(Settings().vlm_provider, "ollama")


class TheScanScreenWithNothingRunning(unittest.TestCase):
    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        self.html = (Path(conrod.__file__).parent / "web" / "index.html").read_text(
            encoding="utf-8")

    def test_it_says_so_rather_than_showing_a_blank_panel(self) -> None:
        self.assertIn("Nothing is running", self.html)
        self.assertIn("Add an album to start scanning", self.html)

    def test_the_note_goes_away_once_something_is_running(self) -> None:
        fn = self.code[self.code.index("function showScanIdle()"):]
        self.assertIn('$("#scan-idle").hidden = running', fn[:400])


class ASecondScanIsRefused(unittest.TestCase):
    """The detector and the GPU are single-tenant. The server already
    refuses a second run; the button should say so rather than being
    pressed and answering with an error."""

    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_scan_it_all_is_disabled_while_one_runs(self) -> None:
        fn = self.code[self.code.index("function reflectRunning(active)"):]
        self.assertIn("scanBtn.disabled = Boolean(active)", fn[:700])

    def test_adding_an_album_is_still_allowed(self) -> None:
        """Indexing is neither the detector nor the GPU -- refusing it too
        was the bug that started this."""
        fn = self.code[self.code.index("function reflectRunning(active)"):]
        self.assertNotIn('"#btn-add"', fn[:700])

    def test_the_home_page_checks_before_offering_it(self) -> None:
        home = self.code[self.code.index("async function loadHome()"):]
        self.assertIn("reflectRunning", home[:500])


if __name__ == "__main__":
    unittest.main()
