"""A cull that stops part way through must say so.

From a real album: 6,221 frames, of which 3,547 were detected and 2,674
were left exactly as they started -- status pending, no error, one clean
cut between IMGC2630 and IMGC2631. Every preview had been extracted, so
the loop handing out frames simply ended early.

What made that "buggy as hell, it just stops working, no error" rather
than an obvious failure is that the app went on describing it as work in
progress. The count was right and the tense was wrong: the review screen
read "Still scanning -- 2,674 frames to go" for ever, on an album where
nothing was scanning and nothing was going to.

Three things here:

    frames nobody looked at are counted apart from frames that failed
    Stop works during preview extraction, which is half an hour of a
      big shoot and used to ignore it completely
    a scan that cannot fit on the disk is refused before it starts,
      rather than dying somewhere in the middle of writing 20 GB
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conrod import pipeline, store


class CountingWhatIsLeft(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "conrod.db")
        self.addCleanup(self.conn.close)
        self.job = store.create_job(self.conn, Path("C:/shoot"), "Bathurst", {})

    def _frames(self, **counts) -> None:
        n = 0
        for status, many in counts.items():
            for _ in range(many):
                n += 1
                path = Path(f"C:/shoot/{n:05d}.CR3")
                store.add_images(self.conn, self.job, [path])
                self.conn.execute(
                    "UPDATE images SET status=? WHERE job_id=? AND path=?",
                    (status, self.job, str(path)))
        self.conn.commit()

    def _job(self) -> dict:
        return dict(store.list_jobs(self.conn)[0])

    def test_frames_nobody_reached_are_the_ones_still_to_do(self) -> None:
        self._frames(detected=3, pending=7)
        self.assertEqual(self._job()["unfinished_count"], 7)

    def test_a_frame_that_failed_is_not_still_to_do(self) -> None:
        """It was looked at. Counting it as pending meant an album with
        fifty unreadable frames said "50 frames to go" for ever, however
        many times it was run -- there was nothing left that running it
        again could change."""
        self._frames(detected=3, error=5)
        self.assertEqual(self._job()["unfinished_count"], 0)

    def test_but_it_is_still_counted_somewhere(self) -> None:
        """Silently dropping them would be the other way to be wrong."""
        self._frames(detected=3, error=5)
        self.assertEqual(self._job()["failed_count"], 5)

    def test_the_album_that_started_this(self) -> None:
        self._frames(detected=3547, pending=2674)
        job = self._job()
        self.assertEqual(job["image_count"], 6221)
        self.assertEqual(job["unfinished_count"], 2674)
        self.assertEqual(job["failed_count"], 0)


class TheReviewScreenSaysWhichItIs(unittest.TestCase):
    """The count alone cannot tell them apart, so the screen asks."""

    def setUp(self) -> None:
        import conrod

        self.js = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_it_asks_whether_a_scan_is_actually_running(self) -> None:
        note = self.js[self.js.index("const note = $(\"#review-note\");"):]
        note = note[:note.index("\n}")]
        self.assertIn("/api/scan", note)
        self.assertIn("running", note)

    def test_still_scanning_is_only_said_while_one_is(self) -> None:
        self.assertIn("left > 0 && running", self.js)

    def test_otherwise_it_offers_to_carry_on(self) -> None:
        self.assertIn("never looked at", self.js)
        self.assertIn("Carry on", self.js)


class StopWorksDuringPreviewExtraction(unittest.TestCase):
    """Six thousand RAWs is about half an hour, and Stop did nothing for
    the whole of it. An app that ignores its own buttons for half an hour
    cannot be told from one that has hung."""

    def test_the_extractor_is_given_the_stop_check(self) -> None:
        import inspect

        from conrod import exif

        self.assertIn("should_stop",
                      inspect.signature(exif.extract_previews).parameters)

    def test_the_pipeline_passes_it_down(self) -> None:
        import inspect

        source = inspect.getsource(pipeline._prepare_previews)
        self.assertIn("should_stop=should_stop", source)

    def test_nothing_is_extracted_once_it_says_stop(self) -> None:
        from conrod import exif

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "previews"
            files = [Path(tmp) / f"{n}.CR3" for n in range(50)]
            for f in files:
                f.write_bytes(b"not really a raw")
            with patch.object(exif, "_run_batch_extract") as batch:
                exif.extract_previews(files, out, executable="exiftool",
                                      chunk=5, workers=1,
                                      should_stop=lambda: True)
            batch.assert_not_called()


class AScanThatCannotFitIsRefusedFirst(unittest.TestCase):
    """Failing at the start is a sentence someone can act on. Failing at
    frame 3,547 of 6,221 is an album that says "Still scanning" for ever."""

    def _room(self, free_gb: float, raws: int):
        import collections

        usage = collections.namedtuple("usage", "total used free")
        said: list[dict] = []
        with patch.object(pipeline.shutil, "disk_usage",
                          return_value=usage(0, 0, int(free_gb * 1024 ** 3))):
            pipeline._check_room([object()] * raws, said.append)
        return said

    def test_a_full_drive_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(pipeline.NotEnoughRoom) as caught:
            self._room(free_gb=2, raws=6000)
        why = str(caught.exception)
        self.assertIn("GB", why)
        self.assertIn("free", why)

    def test_room_to_spare_says_nothing(self) -> None:
        self.assertEqual(self._room(free_gb=500, raws=6000), [])

    def test_only_just_enough_is_worth_a_word(self) -> None:
        """The album this came from had 24.8 GB free and 20 GB of cache
        already written. It fitted, and it was not going to fit twice."""
        said = self._room(free_gb=12, raws=6000)
        self.assertEqual(len(said), 1)
        self.assertIn("not by much", said[0]["message"])

    def test_a_drive_it_cannot_measure_is_not_refused(self) -> None:
        """Not knowing is not a reason to stop someone scanning."""
        with patch.object(pipeline.shutil, "disk_usage", side_effect=OSError):
            pipeline._check_room([object()] * 6000, lambda _e: None)

    def test_the_estimate_comes_from_measured_previews(self) -> None:
        """1.2 MB each, from 16,108 of them on a real machine. A guess
        would make this either useless or a nuisance."""
        self.assertGreater(pipeline.PREVIEW_BYTES, 500_000)
        self.assertLess(pipeline.PREVIEW_BYTES, 3_000_000)


class TheLogDoesNotGrowForEver(unittest.TestCase):
    """It reached 81 MB on a real machine, mostly one line per crop from a
    rate-limited afternoon. At that size it has stopped being the thing you
    read when a scan goes wrong."""

    def test_it_rolls_over_once_it_is_large(self) -> None:
        from conrod import config

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "conrod.log"
            with patch.object(config, "LOG_PATH", log), \
                 patch.object(config, "LOG_MAX_BYTES", 200):
                config.append_log("x" * 500)
                config.append_log("the newest line")
                self.assertTrue(log.with_suffix(".log.1").exists())
                self.assertIn("the newest line", log.read_text(encoding="utf-8"))
                self.assertLess(log.stat().st_size, 200)

    def test_yesterdays_failure_is_still_readable(self) -> None:
        """One roll-over is kept, so a scan that died overnight has not
        been erased by the morning's rate limits."""
        from conrod import config

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "conrod.log"
            with patch.object(config, "LOG_PATH", log), \
                 patch.object(config, "LOG_MAX_BYTES", 200):
                config.append_log("the failure worth keeping" + "x" * 400)
                config.append_log("today")
                kept = log.with_suffix(".log.1").read_text(encoding="utf-8")
                self.assertIn("the failure worth keeping", kept)

    def test_it_never_raises(self) -> None:
        """Every caller is already handling a failure of its own."""
        from conrod import config

        with patch.object(config, "LOG_PATH", Path("Z:/nope/conrod.log")):
            config.append_log("still fine")


if __name__ == "__main__":
    unittest.main()
