"""What can be thrown away, and what it costs to throw it.

The cache reached 20 GB on a real machine with 24.8 GB free, which is how
a scan came to stop half way through an album. Getting the room back is
easy; getting it back without losing something that mattered is the part
worth writing down.

Three kinds of preview, and they are not worth the same:

    in use      a frame of an album that has been looked at. Dropping it
                means that RAW cannot be shown until it is pulled again.
    waiting     already pulled for a frame no scan has reached. Dropping
                it costs the extraction time and nothing else.
    orphaned    belongs to no album at all. Free.

The second is the one a naive sweep gets wrong: a frame's preview_path is
only written once the frame has been through detection, so previews
waiting for an unfinished album are referenced by nothing and look exactly
like rubbish. On the album this came from that was 2,674 files and 6.84
GB, and deleting them would have quietly added a quarter of an hour to
carrying on.

Crops are not offered at all beyond the orphaned ones. A crop carries the
stars, the plate, the number and the embedding; remaking one means
detecting the frame again.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conrod import server, store
from conrod.exif import _mirror_name


class _Cache(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / "cache"
        for sub in ("previews", "crops", "thumbs"):
            (self.cache / sub).mkdir(parents=True)
        self.log = self.root / "conrod.log"

        self.conn = store.connect(self.root / "conrod.db")
        self.addCleanup(self.conn.close)
        self.job = store.create_job(self.conn, Path("C:/shoot"), "Bathurst", {})

        patches = [
            patch.object(server, "CACHE_DIR", self.cache),
            patch.object(server, "LOG_PATH", self.log),
            patch.object(server.store, "session", lambda: _Session(self.conn)),
            patch.dict(server._run, {"active": False}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _preview_for(self, name: str, size: int = 1000) -> Path:
        """Where extraction would put this frame's preview."""
        source = Path("C:/shoot") / name
        dest = self.cache / "previews" / _mirror_name(source.parent)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{source.stem}.jpg"
        out.write_bytes(b"x" * size)
        return out

    def _frame(self, name: str, *, detected: bool) -> int:
        path = Path("C:/shoot") / name
        store.add_images(self.conn, self.job, [path])
        image_id = self.conn.execute(
            "SELECT id FROM images WHERE job_id=? AND path=?",
            (self.job, str(path))).fetchone()[0]
        preview = self._preview_for(name)
        if detected:
            self.conn.execute(
                "UPDATE images SET status='detected', preview_path=? WHERE id=?",
                (str(preview), image_id))
        self.conn.commit()
        return image_id

    def _loose(self, size: int = 500) -> Path:
        """A preview from a folder scanned once and never seen again."""
        dest = self.cache / "previews" / "D__old_shoot_deadbeef"
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"gone{size}.jpg"
        out.write_bytes(b"x" * size)
        return out


class TheSurvey(_Cache):
    def test_it_separates_the_three_kinds(self) -> None:
        self._frame("a.CR3", detected=True)
        self._frame("b.CR3", detected=False)
        self._loose()
        got = server.cache_survey()
        self.assertEqual(got["in_use"]["files"], 1)
        self.assertEqual(got["waiting"]["files"], 1)
        self.assertEqual(got["orphaned"]["files"], 1)

    def test_a_preview_waiting_for_an_unfinished_frame_is_not_rubbish(self) -> None:
        """It is referenced by nothing, because preview_path is only
        written once a frame has been detected. Counting it as orphaned is
        how a sweep silently costs someone the extraction again."""
        self._frame("b.CR3", detected=False)
        got = server.cache_survey()
        self.assertEqual(got["orphaned"]["files"], 0)
        self.assertEqual(got["waiting"]["files"], 1)

    def test_a_crop_an_album_still_uses_is_never_counted(self) -> None:
        image_id = self._frame("a.CR3", detected=True)
        crop = self.cache / "crops" / "00.jpg"
        crop.write_bytes(b"x" * 900)
        store.add_detection(self.conn, image_id, (0, 0, 10, 10), "car", 0.9,
                            str(crop))
        self.conn.commit()
        self.assertEqual(server.cache_survey()["orphaned_crops"]["files"], 0)


class Clearing(_Cache):
    def _clear(self, **fields):
        return server.cache_clear(server.CacheClear(**fields))

    def test_nothing_is_cleared_unless_it_is_asked_for(self) -> None:
        """Every option is off by default, because a cache button that
        guesses will eventually guess wrong about the album someone is in
        the middle of."""
        self._frame("a.CR3", detected=True)
        self._frame("b.CR3", detected=False)
        loose = self._loose()
        out = self._clear()
        self.assertEqual(out["removed"], 0)
        self.assertTrue(loose.exists())

    def test_orphans_go_when_asked(self) -> None:
        loose = self._loose()
        self.assertTrue(self._clear(orphaned=True)["removed"])
        self.assertFalse(loose.exists())

    def test_and_take_nothing_else_with_them(self) -> None:
        used = self._frame("a.CR3", detected=True)
        waiting = self._preview_for("b.CR3")
        self._frame("b.CR3", detected=False)
        self._loose()
        self._clear(orphaned=True)
        row = self.conn.execute("SELECT preview_path FROM images WHERE id=?",
                                (used,)).fetchone()
        self.assertTrue(Path(row["preview_path"]).exists())
        self.assertTrue(waiting.exists())

    def test_clearing_an_album_forgets_where_its_previews_were(self) -> None:
        """Leaving the path behind would make every frame of that album
        offer a preview that is not there."""
        image_id = self._frame("a.CR3", detected=True)
        self._clear(job_id=self.job)
        row = self.conn.execute("SELECT preview_path, status FROM images WHERE id=?",
                                (image_id,)).fetchone()
        self.assertIsNone(row["preview_path"])
        self.assertEqual(row["status"], "detected")

    def test_clearing_an_album_keeps_its_vehicles_and_stars(self) -> None:
        image_id = self._frame("a.CR3", detected=True)
        crop = self.cache / "crops" / "00.jpg"
        crop.write_bytes(b"x" * 900)
        det = store.add_detection(self.conn, image_id, (0, 0, 10, 10), "car",
                                  0.9, str(crop))
        self.conn.execute("UPDATE detections SET stars=5 WHERE id=?", (det,))
        self.conn.commit()

        self._clear(job_id=self.job)
        row = self.conn.execute(
            "SELECT stars, crop_path FROM detections WHERE id=?", (det,)).fetchone()
        self.assertEqual(row["stars"], 5)
        self.assertTrue(Path(row["crop_path"]).exists())

    def test_the_frames_waiting_can_be_dropped_on_purpose(self) -> None:
        waiting = self._preview_for("b.CR3")
        self._frame("b.CR3", detected=False)
        self._clear(waiting=True)
        self.assertFalse(waiting.exists())

    def test_the_log_goes_only_when_ticked(self) -> None:
        self.log.write_bytes(b"x" * 100)
        self._clear(orphaned=True)
        self.assertEqual(self.log.stat().st_size, 100)
        self._clear(log=True)
        self.assertEqual(self.log.stat().st_size, 0)

    def test_the_log_is_emptied_rather_than_deleted(self) -> None:
        """A windowed build sends its output there and holds it open for
        the whole session, and Windows will not unlink a file somebody has
        open -- so deleting it failed silently and the 78 MB stayed put."""
        self.log.write_bytes(b"x" * 100)
        with open(self.log, "a", encoding="utf-8"):     # as the app holds it
            out = self._clear(log=True)
        self.assertTrue(self.log.exists())
        self.assertEqual(self.log.stat().st_size, 0)
        self.assertEqual(out["freed"], 100)

    def test_the_rolled_over_copy_is_deleted(self) -> None:
        """Nothing has that one open, and keeping it would leave most of
        the room still gone."""
        old = self.log.with_suffix(".log.1")
        old.write_bytes(b"x" * 4000)
        self._clear(log=True)
        self.assertFalse(old.exists())

    def test_it_refuses_while_a_scan_is_running(self) -> None:
        """The scan is writing into the very directory this would empty."""
        from fastapi import HTTPException

        with patch.dict(server._run, {"active": True}):
            with self.assertRaises(HTTPException) as caught:
                self._clear(orphaned=True)
        self.assertEqual(caught.exception.status_code, 409)

    def test_it_reports_what_it_freed(self) -> None:
        self._loose(size=4000)
        self.assertEqual(self._clear(orphaned=True)["freed"], 4000)


class TheMessageThatPointsHere(unittest.TestCase):
    def test_the_refusal_names_something_that_exists(self) -> None:
        """It named a "Clear cached previews" button that had never been
        built -- an error message sending someone to look for a control
        that is not there is worse than one that says nothing."""
        import inspect

        from conrod import pipeline

        said = inspect.getsource(pipeline._check_room)
        self.assertIn("Cached previews", said)
        page = (Path(server.WEB_DIR) / "index.html").read_text(encoding="utf-8")
        self.assertIn("Cached previews", page)


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
