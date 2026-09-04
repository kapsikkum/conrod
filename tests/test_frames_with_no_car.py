"""A photograph with no vehicle in it still gets an opinion.

The cull measures the car, which is the right question when there is one.
When the detector finds nothing there is no crop, so there was no score, no
verdict and no stars -- and "the detector found nothing" is not the same as
"this frame is fine". A run of empty frames came back with no verdict of any
kind and sorted alongside the keepers.

Measured on the whole frame instead, which is the honest fallback: with no
subject to point at there is nothing to measure but the picture.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from conrod import pipeline
from conrod.config import Settings


def _photo(width=600, height=400, blur=0.0, seed=7):
    rng = np.random.default_rng(seed)
    data = np.full((height, width), 40, dtype=np.uint8)
    for _ in range(70):
        x, y = rng.integers(0, width - 60), rng.integers(0, height - 50)
        data[y:y + rng.integers(6, 50), x:x + rng.integers(8, 60)] = (
            rng.integers(90, 235))
    image = Image.fromarray(data, mode="L").convert("RGB")
    return image if not blur else image.filter(ImageFilter.GaussianBlur(blur))


class TheWholeFrameIsMeasured(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE images (id INTEGER PRIMARY KEY, sharpness REAL,"
            " rating REAL, rating_verdict TEXT);"
            "INSERT INTO images (id) VALUES (1);")
        self.tmp = Path(__file__).parent / "_nocar.jpg"

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def _rate(self, image) -> sqlite3.Row:
        image.save(self.tmp, quality=95)
        pipeline._rate_whole_frame(self.conn, 1, self.tmp, Settings())
        return self.conn.execute("SELECT * FROM images WHERE id=1").fetchone()

    def test_a_sharp_frame_with_no_car_is_still_rated(self) -> None:
        row = self._rate(_photo())
        self.assertIsNotNone(row["rating"])
        self.assertIsNotNone(row["rating_verdict"])

    def test_a_soft_frame_scores_below_a_sharp_one(self) -> None:
        sharp = self._rate(_photo())["rating"]
        self.conn.execute("UPDATE images SET rating=NULL WHERE id=1")
        soft = self._rate(_photo(blur=2))["rating"]
        self.assertGreater(sharp, soft)

    def test_a_frame_with_no_texture_anywhere_is_left_unrated(self) -> None:
        """The limit of measuring a whole frame, written down.

        Blurring something flattens it, so past a point every tile falls
        under the contrast floor and the measure cannot tell a frame blurred
        to mush from a legitimately empty one -- a sky, a wall, a smear. It
        says nothing rather than guessing, which is the same answer it gives
        for a crop it cannot read.
        """
        row = self._rate(_photo(blur=5))
        self.assertIsNone(row["rating"])

    def test_an_unreadable_frame_is_not_an_error(self) -> None:
        """A bad preview is a frame with no score, not a scan that stops."""
        pipeline._rate_whole_frame(self.conn, 1, Path("nowhere.jpg"), Settings())
        row = self.conn.execute("SELECT * FROM images WHERE id=1").fetchone()
        self.assertIsNone(row["rating"])


class ItIsNotMixedInWithTheCars(unittest.TestCase):
    def test_it_is_stored_on_the_image_not_as_a_detection(self) -> None:
        """A whole-frame score and a subject score are different questions.

        Putting them on one scale would let a sharp photograph of an empty
        piece of track outrank a car.
        """
        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _rate_whole_frame"):
                      source.index("def _record_origins")]
        self.assertIn("UPDATE images SET", body)
        self.assertNotIn("INSERT INTO detections", body)

    def test_the_writer_reaches_frames_with_no_detection(self) -> None:
        """An inner join dropped them, so the rating never reached the file."""
        self.assertIn("LEFT JOIN detections", pipeline.WRITE_QUERY)
        self.assertIn("COALESCE(d.rating, i.rating)", pipeline.WRITE_QUERY)


if __name__ == "__main__":
    unittest.main()
