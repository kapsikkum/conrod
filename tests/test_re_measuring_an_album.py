"""Measuring an album again, without reading the photographs.

`pipeline.rescore` has existed since the focus scale was re-derived, and
nothing could reach it: no endpoint, no button, no menu, no command. So an
album scored against an older scale -- or against a measure with a fault in
it -- could only be corrected by scanning it again from the RAWs. Hours, to
redo a job that takes minutes from the crops already on disk.

Found the hard way. A fix to the measure meant 541 detections of a real
album had been stored with no rating at all, and there was no way to ask
the app to look at them again; it took a hand-written script against the
database. That is a repair a photographer cannot make.
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import conrod
from conrod import pipeline, server


class TheAppCanAskForIt(unittest.TestCase):
    def test_there_is_an_endpoint(self) -> None:
        routes = {getattr(r, "path", "") for r in server.app.routes}
        self.assertIn("/api/jobs/{job_id}/rescore", routes)

    def test_there_is_a_button_wired_to_it(self) -> None:
        web = Path(conrod.__file__).parent / "web"
        page = (web / "index.html").read_text(encoding="utf-8")
        js = (web / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="btn-rescore"', page)
        self.assertIn('$("#btn-rescore").onclick', js)
        self.assertIn("/rescore", js)

    def test_it_is_disabled_with_no_album(self) -> None:
        js = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        block = js[js.index("const ALBUM_ACTIONS"):]
        self.assertIn("#btn-rescore", block[:300])


class WhatItDoesAndDoesNot(unittest.TestCase):
    def test_it_picks_the_keepers_again_afterwards(self) -> None:
        """The stars have moved, so which frame is the keeper of its pass
        may have moved with them. Leaving the old picks would mean an album
        whose blue labels point at frames that are no longer the best."""
        with patch.object(pipeline, "rescore", return_value=7) as measured, \
             patch.object(pipeline, "pick_of_pass",
                          return_value={"passes": 2, "picks": 2,
                                        "considered": 7}) as picked, \
             patch.dict(server._run, {"active": False}):
            out = server.rescore_album(1)
        measured.assert_called_once()
        picked.assert_called_once()
        self.assertEqual(out["rescored"], 7)
        self.assertEqual(out["picks"], 2)

    def test_it_refuses_while_a_scan_is_running(self) -> None:
        from fastapi import HTTPException

        with patch.dict(server._run, {"active": True}):
            with self.assertRaises(HTTPException) as caught:
                server.rescore_album(1)
        self.assertEqual(caught.exception.status_code, 409)

    def test_it_reads_no_photographs(self) -> None:
        """The whole point: it works from the crops, so it costs minutes
        rather than the hours a fresh scan would."""
        source = inspect.getsource(pipeline.rescore)
        self.assertIn("crop_path", source)
        for reading_files in ("extract_previews", "detect_mod.detect",
                              "scan("):
            self.assertNotIn(reading_files, source)

    def test_it_also_measures_frames_with_no_vehicle(self) -> None:
        """They have no crop, so a pass that only walks crops skips them
        entirely -- and on an album culled before the whole-frame fallback
        existed there were 149 such frames carrying no rating at all, with
        no way to fill them in short of scanning the photographs again.

        This is where a detail shot lands when the detector does not
        recognise it as a car: a close-up of a wheel, a badge, an exhaust.
        """
        source = inspect.getsource(pipeline.rescore)
        self.assertIn("_rate_whole_frame", source)
        self.assertIn("NOT EXISTS", source)

    def test_it_leaves_frames_that_already_have_one(self) -> None:
        """Re-running must not undo a whole-frame rating, and must not cost
        a re-read of every empty frame in the album each time."""
        source = inspect.getsource(pipeline.rescore)
        self.assertIn("i.rating IS NULL", source)

    def test_a_star_given_by_hand_is_not_touched(self) -> None:
        """It is in a different column and was never on this scale."""
        source = inspect.getsource(pipeline.rescore)
        self.assertNotIn("SET stars", source)
        self.assertNotIn("stars=", source)


if __name__ == "__main__":
    unittest.main()
