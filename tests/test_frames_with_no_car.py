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

        With nothing in the picture at all -- a frame of sky, a lens cap,
        an unexposed sheet -- every tile falls under the contrast floor,
        no tile survives, and there is nothing to take a percentile of. It
        says nothing rather than guessing.

        Deliberately a flat field rather than a heavily blurred photograph.
        Blur was the fixture here and it does not reach this case: blurring
        the texture below flattens it but leaves tiles above the floor, so
        the measure still has an answer -- see the test below, which is
        about what that answer should be.
        """
        flat = Image.fromarray(
            np.full((400, 600), 128, dtype=np.uint8), mode="L").convert("RGB")
        row = self._rate(flat)
        self.assertIsNone(row["rating"])

    def test_a_frame_blurred_past_the_bottom_of_the_scale_is_still_rated(self) -> None:
        """Zero is an answer, and it used to be thrown away.

        The scale bottoms out: a frame blurred past its low end normalises
        to exactly 0.0, and that was read as "could not be judged" -- so the
        worst frames of a shoot came back with no rating, no verdict and no
        colour, and sorted alongside the ones nobody had looked at yet. On a
        real album that was 541 detections, 8% of everything the cull kept.
        """
        row = self._rate(_photo(blur=5))
        self.assertIsNotNone(row["rating"])
        self.assertEqual(row["rating_verdict"], "poor")

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
