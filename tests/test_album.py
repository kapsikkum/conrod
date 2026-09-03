"""The album screen: what it shows, and what it costs to show it.

Adding a folder used to land on a progress bar, after which the two things
worth doing next -- cull it, or identify it -- were small buttons on a card
back on the home screen. The reading of the folder is the cheap part and it
was getting the whole screen.

Two things had to exist before the album could be the subject: a listing of
the frames themselves (an indexed album has no vehicles, so the vehicle grid
had nothing to show and the album looked empty when it was not), and
thumbnails.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from conrod import server


class WhatTheCullSays(unittest.TestCase):
    """The live view during a cull.

    A frame with vehicles waits for the vision model to say something about
    them before it is shown. A cull never calls the vision model, so nothing
    ever arrived: the live view showed only the frames where nothing was
    found -- four tiles reading NO VEHICLE while the counter beside them
    climbed past fifty vehicles. The cull had plenty to say.
    """

    def test_a_frame_with_vehicles_reports_the_sharpness_work(self) -> None:
        boxes = [{"kind": "car", "rating": "good", "focus": "sharp", "stars": 4}]
        self.assertEqual(server._frame_phase(boxes, True), "CHECKING SHARPNESS")
        log = server._frame_log(boxes, True)
        self.assertEqual(log[0], "1 vehicle detected")
        self.assertIn("sharpness", log[1])
        self.assertIn("4 stars", log[2])

    def test_a_culled_vehicle_says_so(self) -> None:
        log = server._frame_log(
            [{"kind": "car", "rating": "poor", "culled": True}], True)
        self.assertTrue(any("culled" in line for line in log))

    def test_a_panning_shot_says_it_was_kept(self) -> None:
        """A held pan is blur in the background, not on the car. Saying so is
        the difference between "it understood the shot" and "it got it
        wrong" -- and this is the complaint that started the whole cull
        rework."""
        log = server._frame_log(
            [{"kind": "car", "rating": "poor", "panning": True}], True)
        line = [l for l in log if "panning" in l]
        self.assertTrue(line, log)
        self.assertIn("kept", line[0])

    def test_an_empty_frame_still_says_so_plainly(self) -> None:
        self.assertEqual(server._frame_phase([], True), "NO VEHICLE")
        self.assertEqual(server._frame_log([], True),
                         ["no vehicle found in this frame"])

    def test_identifying_keeps_the_old_short_line(self) -> None:
        """When the vision model is coming, it does the talking."""
        boxes = [{"kind": "car"}, {"kind": "car"}]
        self.assertEqual(server._frame_phase(boxes, False), "VEHICLES FOUND")
        self.assertEqual(server._frame_log(boxes, False), ["2 vehicles detected"])


class Thumbnails(unittest.TestCase):
    """A contact sheet cannot be built out of the previews themselves.

    The stored preview is JpgFromRaw -- 4640x6960 and about 6MB, that size on
    purpose because a registration plate has to survive in it. Measured on a
    real album: 200 of those is 1.2GB over the wire, and the page could not
    finish rendering.
    """

    def _preview(self, folder: Path) -> Path:
        """Something shaped like a CR3 preview: big, and portrait."""
        source = folder / "frame.jpg"
        Image.new("RGB", (1160, 1740), (90, 110, 140)).save(
            source, "JPEG", quality=92)
        return source

    def test_it_is_very_much_smaller_than_the_preview(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._preview(folder)
            thumb = server.make_thumb(source, folder / "t" / "1.jpg")

            self.assertTrue(thumb.exists())
            self.assertLess(thumb.stat().st_size, source.stat().st_size / 5)
            with Image.open(thumb) as image:
                self.assertLessEqual(max(image.size), server.THUMB_EDGE)

    def test_the_shape_of_the_frame_is_kept(self) -> None:
        """Portrait frames are most of a motorsport shoot once the camera is
        turned, and a squashed contact sheet is unreadable."""
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            thumb = server.make_thumb(self._preview(folder), folder / "1.jpg")
            with Image.open(thumb) as image:
                width, height = image.size
            self.assertAlmostEqual(width / height, 1160 / 1740, places=1)

    def test_a_second_request_does_not_rebuild_it(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._preview(folder)
            first = server.make_thumb(source, folder / "1.jpg")
            stamp = first.stat().st_mtime_ns
            server.make_thumb(source, folder / "1.jpg")
            self.assertEqual(first.stat().st_mtime_ns, stamp)

    def test_a_re_extracted_preview_is_not_served_stale(self) -> None:
        import os
        import time

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._preview(folder)
            cached = server.make_thumb(source, folder / "1.jpg")
            before = cached.stat().st_mtime_ns

            time.sleep(0.01)
            Image.new("RGB", (1160, 1740), (200, 40, 40)).save(source, "JPEG")
            os.utime(source, None)

            server.make_thumb(source, folder / "1.jpg")
            self.assertNotEqual(cached.stat().st_mtime_ns, before)


class TheFrameListing(unittest.TestCase):
    """The query behind the contact sheet."""

    QUERY = """
        SELECT i.id, i.path,
               COUNT(d.id) AS vehicles,
               SUM(CASE WHEN d.rejected THEN 1 ELSE 0 END) AS cut,
               (SELECT d2.rating_verdict FROM detections d2
                 WHERE d2.image_id = i.id AND d2.rejected = 0
                 ORDER BY d2.rating DESC LIMIT 1) AS verdict
          FROM images i
          LEFT JOIN detections d ON d.image_id = i.id
         WHERE i.job_id = 1
         GROUP BY i.id ORDER BY i.id"""

    def _album(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE images (id INTEGER PRIMARY KEY, job_id INT, path TEXT);"
            "CREATE TABLE detections (id INTEGER PRIMARY KEY, image_id INT,"
            " rejected INT DEFAULT 0, rating REAL, rating_verdict TEXT);")
        conn.execute("INSERT INTO images VALUES (1,1,'a.CR3')")
        for n, (rejected, rating, verdict) in enumerate(rows, start=1):
            conn.execute("INSERT INTO detections VALUES (?,1,?,?,?)",
                         (n, rejected, rating, verdict))
        return conn

    def test_a_frame_with_no_vehicles_is_still_listed(self) -> None:
        """The reason this endpoint exists. An indexed album is entirely
        frames with no vehicles, and the review screen showed nothing at
        all."""
        conn = self._album([])
        row = conn.execute(self.QUERY).fetchone()
        self.assertEqual(row["vehicles"], 0)
        self.assertIsNone(row["verdict"])

    def test_the_frame_takes_its_verdict_from_its_best_survivor(self) -> None:
        """The same rule write_job uses to choose the rating it writes. A
        frame with one soft car and one sharp one is a keeper."""
        conn = self._album([(0, 0.20, "poor"), (0, 0.80, "good")])
        self.assertEqual(conn.execute(self.QUERY).fetchone()["verdict"], "good")

    def test_a_culled_vehicle_does_not_speak_for_the_frame(self) -> None:
        conn = self._album([(1, 0.95, "good"), (0, 0.30, "poor")])
        row = conn.execute(self.QUERY).fetchone()
        self.assertEqual(row["verdict"], "poor")
        self.assertEqual(row["cut"], 1)

    def test_kept_is_what_is_left_after_the_cull(self) -> None:
        conn = self._album([(1, 0.1, "poor"), (0, 0.9, "good"), (0, 0.8, "good")])
        row = conn.execute(self.QUERY).fetchone()
        self.assertEqual(row["vehicles"] - row["cut"], 2)


class ReachableFromTheInterface(unittest.TestCase):
    def test_an_album_that_is_not_there_is_refused(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        self.assertEqual(client.get("/api/jobs/999999/frames").status_code, 404)

    def test_the_album_screen_offers_both_decisions(self) -> None:
        web = Path(server.__file__).parent / "web"
        markup = (web / "index.html").read_text(encoding="utf-8")
        for want in ("screen-album", "do-cull", "do-identify", "album-sheet"):
            self.assertIn(want, markup)

        code = (web / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/thumb/", code,
                      "the sheet must use thumbnails, not full previews")
        self.assertNotIn("`/api/frame/${f.id}`", code)


if __name__ == "__main__":
    unittest.main()
