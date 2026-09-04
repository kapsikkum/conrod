"""A scan of a weekend's shooting runs for hours, and that is exactly when
the next card gets added. It could not be, for two separate reasons: the
Scan screen hid its own "add a folder" form the moment a scan started and
never put it back, and had the form been reachable the endpoint would have
refused with "a scan is already running" anyway.

The form has since moved to the home page entirely, into the card that
offers a scan in the first place -- see TheFormLivesOnTheHomePage.

The one-at-a-time rule is about the GPU and the detector. Indexing touches
neither: it walks the folder, reads EXIF and pulls out previews. So it is
allowed alongside a run, on its own thread, reporting into its own slot --
never into `_run`, which owns the live frame view, the pause gate and the
stop flag, none of which mean anything to a folder being read.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import conrod
from conrod import server


class TheFormLivesOnTheHomePage(unittest.TestCase):
    """A scan of a weekend's shooting runs for hours, and that is exactly
    when the next card gets added -- so the picker cannot be somewhere that
    a running scan takes over.

    It briefly lived on the Scan screen, folded under the live view behind
    a "+ Add another album" button: a ghost button on its own in the dark,
    under the thing it was not part of. It is on the home page now, in the
    card that offers a scan in the first place, which opens into the picker
    where it stands.
    """

    def setUp(self):
        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        self.html = (Path(conrod.__file__).parent / "web" / "index.html").read_text(
            encoding="utf-8")

    def test_the_picker_is_in_the_home_card(self) -> None:
        home = self.html[self.html.index('id="screen-home"'):
                         self.html.index('id="screen-album"')]
        self.assertIn('id="scan-setup-body"', home)
        self.assertIn('id="scan-path"', home)
        self.assertIn('id="btn-add"', home)

    def test_the_scan_screen_is_only_the_running_job_now(self) -> None:
        scan = self.html[self.html.index('id="screen-scan"'):
                         self.html.index('id="screen-settings"')]
        self.assertIn('id="scanner"', scan)
        self.assertNotIn('id="scan-path"', scan)
        self.assertNotIn("Add another album", self.html)

    def test_new_scan_opens_it_where_it_stands(self) -> None:
        self.assertIn("function openNewScan(open)", self.code)
        self.assertIn('$("#btn-new-scan").onclick', self.code)

    def test_starting_one_closes_the_picker_behind_it(self) -> None:
        start = self.code[self.code.index("async function startScan(stage)"):]
        self.assertIn("openNewScan(false)", start[:2000])

    def test_adding_alongside_a_run_does_not_seize_the_run_panel(self) -> None:
        """The progress on screen belongs to the other job. Showing it for
        this one -- with its stop and pause buttons -- would stop the wrong
        scan."""
        start = self.code[self.code.index("async function startScan(stage)"):]
        body = start[:start.index('$("#btn-add")')]
        self.assertIn("if (res.indexing)", body)
        seize = body.index('$("#scanner").hidden = false;')
        self.assertLess(body.index("if (res.indexing)"), seize,
                        "the indexing case must return before the panel is seized")


class TheSecondJobIsKeptApart(unittest.TestCase):
    def test_indexing_has_its_own_progress_slot(self) -> None:
        for key in ("active", "job_id", "done", "total", "message", "error"):
            self.assertIn(key, server._index)

    def test_the_slot_starts_idle(self) -> None:
        self.assertFalse(server._index["active"])

    def test_status_reports_it_beside_the_run(self) -> None:
        out = server.scan_status()
        self.assertIn("indexing", out)
        self.assertIn("active", out["indexing"])

    def test_only_indexing_is_let_through_while_a_scan_runs(self) -> None:
        """Culling and identifying are what the rule exists for -- they are
        the GPU. Those still wait their turn."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        guard = source[source.index("def start_scan(body: ScanRequest)"):]
        guard = guard[:guard.index("_frames.clear()")]
        self.assertIn('if body.stage == "index" and not _index["active"]:', guard)
        self.assertIn('raise HTTPException(409, "a scan is already running")', guard)

    def test_the_concurrent_index_stops_before_detection(self) -> None:
        """stop_after="index" is what makes it safe to run beside a scan --
        past that point it would want the detector, which is single-tenant."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _start_index(body: ScanRequest)"):]
        body = body[:body.index("@app.")]      # this function only
        self.assertIn('stop_after="index"', body)
        self.assertNotIn("_run[", body)


if __name__ == "__main__":
    unittest.main()
