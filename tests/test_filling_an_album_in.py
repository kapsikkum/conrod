"""An album is the file list, and the frames fill in behind it.

Adding a shoot used to take about twenty-five minutes before the first
thumbnail appeared. Four walks of the whole folder, all blocking: read every
rating, read every camera and capture time, read every sidecar, extract
every preview. Measured on a 7,337-frame shoot on an external drive.

None of that work was wrong, only scheduled wrong. Per frame it is small --
137 ms to extract a preview in a batch, 28 ms to read its tags -- so it
belongs behind the album rather than in front of it. Adding one is now the
folder walk and nothing else: 1.7 seconds for those 7,337 frames.

What replaces it, and what these tests hold in place:

* asking for a thumbnail is what fills a frame in, and it fills the frames
  around it too, because starting exiftool is most of the cost
* the on-demand path takes the picture only. The camera, capture time and
  rating are wanted by the cull, which is minutes away, and making somebody
  wait for them while they scroll spends their time on the wrong thing
* a background pass picks up everything else, including the EXIF the
  on-demand path skipped, and stands aside whenever a tile is waiting
* burst numbers cannot be settled a chunk at a time, so they are rebuilt
  from the stored capture times
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conrod import pipeline, server, store
from conrod.config import Settings


class AddingAnAlbumOnlyWalksTheFolder(unittest.TestCase):
    def test_it_does_no_exif_and_no_extraction(self) -> None:
        """The four passes that made it twenty-five minutes are gone from
        the path that stops at "index"."""
        import inspect

        source = inspect.getsource(pipeline.run)
        index_return = source.index('stop_after == "index"')
        before = source[:index_return]
        for expensive in ("_record_origins(", "_prepare_previews("):
            self.assertNotIn(expensive, before,
                             f"{expensive} still runs before an album opens")

    def test_existing_ratings_are_not_read_up_front(self) -> None:
        """That read is two minutes on a big folder, and its answer only
        matters when something is about to be culled or written."""
        import inspect

        source = inspect.getsource(pipeline.run)
        self.assertIn('respect_culling and stop_after != "index"', source)

    def test_the_walk_does_not_stat_every_entry(self) -> None:
        """rglob asks the filesystem whether each entry is a file, one at a
        time; a directory listing already knows. 33 seconds against 0.13 on
        the folder this came from, and the walk is now the whole job."""
        import inspect

        source = inspect.getsource(pipeline.scan)
        # The body, not the docstring -- which mentions rglob to say why it
        # is not used.
        body = source[source.index('"""', source.index('"""') + 3):]
        self.assertIn("os.walk", body)
        self.assertNotIn("rglob", body)


class _Album(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "conrod.db")
        self.addCleanup(self.conn.close)
        self.job = store.create_job(self.conn, Path("D:/shoot"), "meet", {})
        self.ids = []
        for n in range(80):
            path = Path(f"D:/shoot/{n:05d}.CR3")
            store.add_images(self.conn, self.job, [path])
            self.ids.append(self.conn.execute(
                "SELECT id FROM images WHERE path=?", (str(path),)).fetchone()[0])
        self.conn.commit()

        patches = [
            patch.object(server.store, "connect", lambda *a, **k: self.conn),
            patch.object(server.store, "session", lambda: _Session(self.conn)),
            patch.dict(server._state, {"settings": Settings()}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class OneRequestFillsARunOfFrames(_Album):
    def test_it_extracts_the_neighbours_not_just_the_one(self) -> None:
        """A frame on its own is 1,364 ms and a batch is a fraction of that
        each -- almost all of it starting exiftool."""
        seen = {}

        def fake(files, out, **kwargs):
            seen["count"] = len(list(files))
            return {Path(f): Path(f"{f}.jpg") for f in files}

        with patch.object(server.exif, "extract_previews", fake):
            filled = server._fill_run(self.job, self.ids[10],
                                      size=8, tags=False)
        self.assertEqual(filled, 8)
        self.assertEqual(seen["count"], 8)

    def test_the_on_demand_path_skips_the_exif(self) -> None:
        """A tile needs the picture. The camera and capture time are for the
        cull, which is minutes away."""
        with patch.object(server.exif, "extract_previews",
                          lambda files, out, **k: {Path(f): Path(f"{f}.jpg")
                                                   for f in files}), \
             patch.object(server.pipeline, "fill_frames") as tags:
            server._fill_run(self.job, self.ids[0], size=8, tags=False)
        tags.assert_not_called()

    def test_the_background_path_reads_it(self) -> None:
        with patch.object(server.exif, "extract_previews",
                          lambda files, out, **k: {Path(f): Path(f"{f}.jpg")
                                                   for f in files}), \
             patch.object(server.pipeline, "fill_frames") as tags:
            server._fill_run(self.job, self.ids[0], size=8, tags=True)
        tags.assert_called_once()

    def test_a_second_pass_over_the_same_frames_does_nothing(self) -> None:
        """So a thumbnail request that lands where the background pass has
        already been costs a database read and no exiftool at all."""
        with patch.object(server.exif, "extract_previews",
                          lambda files, out, **k: {Path(f): Path(f"{f}.jpg")
                                                   for f in files}):
            server._fill_run(self.job, self.ids[0], size=8, tags=False)
            again = server._fill_run(self.job, self.ids[0], size=8, tags=False)
        self.assertEqual(again, 0)

    def test_a_frame_whose_preview_will_not_come_is_not_retried_for_ever(self) -> None:
        """A corrupt RAW, a file that has gone, a preview exiftool will not
        give up. Left alone it would hold the background pass at the same run
        and every frame past it would never be reached.

        Marked as tried with an empty string, which the thumb endpoint reads
        as "looked at, and there is nothing" -- distinct from NULL, which is
        "not looked at yet".
        """
        server._mark_unfillable(self.job, self.ids[0])
        marked = self.conn.execute(
            "SELECT preview_path FROM images WHERE id=?",
            (self.ids[0],)).fetchone()
        self.assertEqual(marked["preview_path"], "")

        called = []
        with patch.object(server.exif, "extract_previews",
                          lambda files, out, **k: called.append(1) or {}):
            self.assertEqual(
                server._fill_run(self.job, self.ids[0], size=8, tags=False), 0)
        self.assertEqual(called, [], "a frame already tried must not be reopened")

    def test_marking_only_covers_the_run_that_failed(self) -> None:
        """Everything past it is still waiting its turn, not written off."""
        server._mark_unfillable(self.job, self.ids[0])
        left = self.conn.execute(
            "SELECT COUNT(*) FROM images WHERE job_id=? AND preview_path IS NULL",
            (self.job,)).fetchone()[0]
        self.assertEqual(left, 80 - server.FILL_CHUNK)


class TheBackgroundPassStandsAside(unittest.TestCase):
    def test_it_waits_while_a_tile_is_being_filled(self) -> None:
        """Unchecked it fills about eight frames a second and turns a
        one-second thumbnail request into four."""
        import inspect

        source = inspect.getsource(server._fill_loop)
        self.assertIn("_foreground_idle", source)

    def test_it_waits_while_a_scan_is_running(self) -> None:
        import inspect

        source = inspect.getsource(server._fill_loop)
        self.assertIn('_run.get("active")', source)

    def test_the_gate_reopens_when_the_last_request_finishes(self) -> None:
        self.assertTrue(server._foreground_idle.is_set())
        with server._in_foreground():
            self.assertFalse(server._foreground_idle.is_set())
            with server._in_foreground():
                self.assertFalse(server._foreground_idle.is_set())
            self.assertFalse(server._foreground_idle.is_set())
        self.assertTrue(server._foreground_idle.is_set())

    def test_it_picks_up_frames_the_on_demand_path_left_untagged(self) -> None:
        """They have a picture and no capture time. Nothing else would ever
        come back for them, and the cull needs them for its bursts."""
        import inspect

        source = inspect.getsource(server._fill_loop)
        self.assertIn("taken_at IS NULL", source)


class BurstsAreSettledAcrossTheWholeAlbum(_Album):
    def _times(self, spec) -> None:
        for n, (camera, taken) in enumerate(spec):
            self.conn.execute(
                "UPDATE images SET camera=?, taken_at=? WHERE id=?",
                (camera, taken, self.ids[n]))
        self.conn.commit()

    def test_frames_seconds_apart_are_one_burst(self) -> None:
        self._times([("A", 100.0), ("A", 100.5), ("A", 101.0)])
        runs = pipeline.rebuild_bursts(self.conn, self.job, Settings())
        self.assertEqual(runs, 1)

    def test_a_long_gap_starts_another(self) -> None:
        self._times([("A", 100.0), ("A", 100.5), ("A", 400.0)])
        self.assertEqual(
            pipeline.rebuild_bursts(self.conn, self.job, Settings()), 2)

    def test_two_cameras_are_never_one_burst(self) -> None:
        """Two shooters interleave into one folder and fire at the same car
        seconds apart."""
        self._times([("A", 100.0), ("B", 100.2), ("A", 100.4)])
        self.assertEqual(
            pipeline.rebuild_bursts(self.conn, self.job, Settings()), 2)

    def test_frames_with_no_time_yet_are_left_out(self) -> None:
        """Half a filled album must not be numbered as though it were all
        of it."""
        self._times([("A", 100.0), ("A", 100.5)])
        pipeline.rebuild_bursts(self.conn, self.job, Settings())
        unfilled = self.conn.execute(
            "SELECT burst_key FROM images WHERE id=?", (self.ids[40],)).fetchone()
        self.assertIsNone(unfilled["burst_key"])

    def test_a_cull_settles_them_before_anything_uses_them(self) -> None:
        """Grouping, the keeper of each pass and the burst read all take
        burst numbers at face value, and a browsed album carries provisional
        ones."""
        import inspect

        source = inspect.getsource(pipeline.run)
        self.assertIn("rebuild_bursts(conn, job_id, settings)", source)


class _Session:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_exc):
        self.conn.commit()
        return False


if __name__ == "__main__":
    unittest.main()
