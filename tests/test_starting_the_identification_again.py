"""Getting a second attempt at an album the model refused.

Identify only looks at detections that have never been answered for, which
is what makes running it twice cheap. The cost of that is a run where the
provider failed leaves an album that is *finished*: every car has an empty
answer, and running Identify again reads nothing and reports success.

There was already a reset, and it was the wrong one to reach for. It
deletes the detections and their crops -- with the stars, the sharpness,
the plates, the numbers and the embeddings attached to them. Re-detecting
a six-thousand-frame album to recover from a typo in a model name is a
poor trade, and losing an afternoon of hand-given stars to it is not a
trade at all.

Also here: whose ratings the learned scale is allowed to learn from. A
star set in Lightroom is a star set by hand.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conrod import store


class _Album(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "conrod.db")
        self.addCleanup(self.conn.close)
        self.job = store.create_job(self.conn, Path("C:/shoot"), "Bathurst", {})

    def _frame(self, name: str, rating=None):
        path = Path("C:/shoot") / name
        store.add_images(self.conn, self.job, [path])
        image_id = self.conn.execute(
            "SELECT id FROM images WHERE job_id=? AND path=?",
            (self.job, str(path))).fetchone()[0]
        if rating is not None:
            self.conn.execute("UPDATE images SET rating_in_file=? WHERE id=?",
                              (rating, image_id))
        return image_id

    def _car(self, image_id: int, **fields):
        det_id = store.add_detection(self.conn, image_id, (0, 0, 100, 100),
                                     "car", 0.9, "C:/crops/00.jpg")
        for column, value in fields.items():
            self.conn.execute(f"UPDATE detections SET {column}=? WHERE id=?",
                              (value, det_id))
        return det_id


class ResettingTheIdentificationsOnly(_Album):
    def _reset(self):
        from conrod import server

        real = server.store.session
        server.store.session = lambda: _Session(self.conn)
        try:
            return server.reset_identifications(job_id=self.job)
        finally:
            server.store.session = real

    def test_what_the_model_said_is_gone(self) -> None:
        det = self._car(self._frame("a.jpg"), attributes='{"make": "Ford"}',
                        group_key=7, group_size=3, group_agreement=1.0)
        self._reset()
        row = self.conn.execute(
            "SELECT attributes, group_key FROM detections WHERE id=?",
            (det,)).fetchone()
        self.assertIsNone(row["attributes"])
        self.assertIsNone(row["group_key"])

    def test_the_car_and_its_crop_are_not(self) -> None:
        """The expensive half. Re-detecting six thousand frames to undo a
        typo in a model name is not a repair, it is a second scan."""
        det = self._car(self._frame("a.jpg"), attributes='{"make": "Ford"}')
        self._reset()
        row = self.conn.execute(
            "SELECT crop_path, cls, conf FROM detections WHERE id=?",
            (det,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["crop_path"], "C:/crops/00.jpg")

    def test_the_stars_survive(self) -> None:
        """They were given by hand, one frame at a time, and nothing can
        recompute them."""
        det = self._car(self._frame("a.jpg"), attributes="{}", stars=4)
        self._reset()
        self.assertEqual(self.conn.execute(
            "SELECT stars FROM detections WHERE id=?", (det,)).fetchone()[0], 4)

    def test_so_do_the_sharpness_and_the_embedding(self) -> None:
        det = self._car(self._frame("a.jpg"), attributes="{}",
                        sharpness=0.81, embedding="0.1,0.2")
        self._reset()
        row = self.conn.execute(
            "SELECT sharpness, embedding FROM detections WHERE id=?",
            (det,)).fetchone()
        self.assertAlmostEqual(row["sharpness"], 0.81)
        self.assertEqual(row["embedding"], "0.1,0.2")

    def test_a_plate_read_off_the_photograph_stays(self) -> None:
        """Plates and roundel numbers never went near the vision model,
        and are the better reading anyway."""
        det = self._car(self._frame("a.jpg"), attributes="{}", plate="39432J",
                        number="62", number_source="roundel", number_conf=0.96)
        self._reset()
        row = self.conn.execute(
            "SELECT plate, number, number_source FROM detections WHERE id=?",
            (det,)).fetchone()
        self.assertEqual(row["plate"], "39432J")
        self.assertEqual(row["number"], "62")

    def test_a_number_the_model_read_does_not(self) -> None:
        det = self._car(self._frame("a.jpg"), attributes="{}", number="8",
                        number_source="vlm", number_conf=0.4)
        self._reset()
        row = self.conn.execute(
            "SELECT number, number_source FROM detections WHERE id=?",
            (det,)).fetchone()
        self.assertIsNone(row["number"])
        self.assertIsNone(row["number_source"])

    def test_the_album_is_ready_to_identify_again(self) -> None:
        """Not "indexed", which would offer to walk the folder again, and
        not "analysed", which would say there is nothing left to do."""
        self._car(self._frame("a.jpg"), attributes="{}")
        self._reset()
        self.assertEqual(self.conn.execute(
            "SELECT status FROM jobs WHERE id=?", (self.job,)).fetchone()[0],
            "culled")


class WhichRatingsTheScaleLearnsFrom(_Album):
    """The learner read detections.stars and nothing else.

    So an album whose ratings had been read back out of the photographs --
    every album, after a reset -- offered two thousand stars and reported
    "0 rated frames so far; 200 needed".
    """

    def _training_rows(self):
        rows = self.conn.execute(
            """SELECT COALESCE(d.stars, i.rating_in_file) AS stars, d.embedding
                 FROM detections d JOIN images i ON i.id = d.image_id
                WHERE d.embedding IS NOT NULL AND d.embedding != ''
                  AND (d.stars IS NOT NULL
                       OR (i.rating_in_file > 0 AND 1 =
                           (SELECT COUNT(*) FROM detections d2
                             WHERE d2.image_id = i.id)))""").fetchall()
        return [r["stars"] for r in rows]

    def test_a_star_set_in_lightroom_counts(self) -> None:
        self._car(self._frame("a.jpg", rating=4), embedding="0.1,0.2")
        self.assertEqual(self._training_rows(), [4])

    def test_a_star_given_here_still_wins(self) -> None:
        """The one in the app is the later answer and the more specific
        one: it is about this car, not about the photograph."""
        self._car(self._frame("a.jpg", rating=4), stars=2, embedding="0.1,0.2")
        self.assertEqual(self._training_rows(), [2])

    def test_a_frame_with_two_cars_in_it_teaches_nothing(self) -> None:
        """The file's star is a judgement on the photograph. With three
        cars in it there is no telling which of them earned it, and giving
        it to all three teaches that a car nobody was asked about is worth
        five."""
        frame = self._frame("a.jpg", rating=5)
        self._car(frame, embedding="0.1,0.2")
        self._car(frame, embedding="0.3,0.4")
        self.assertEqual(self._training_rows(), [])

    def test_but_a_star_given_here_works_on_one_of_them(self) -> None:
        frame = self._frame("a.jpg", rating=5)
        self._car(frame, stars=3, embedding="0.1,0.2")
        self._car(frame, embedding="0.3,0.4")
        self.assertEqual(self._training_rows(), [3])

    def test_an_unrated_frame_is_not_a_zero(self) -> None:
        """Absent is not the same claim as bad, and training on it as one
        would drag every prediction down."""
        self._car(self._frame("a.jpg"), embedding="0.1,0.2")
        self._car(self._frame("b.jpg", rating=0), embedding="0.3,0.4")
        self.assertEqual(self._training_rows(), [])

    def test_a_car_with_no_embedding_cannot_be_learned_from(self) -> None:
        self._car(self._frame("a.jpg", rating=5))
        self.assertEqual(self._training_rows(), [])

    def test_the_pipeline_asks_the_same_question(self) -> None:
        """The query above is the one that ships, not a copy of it that
        drifted."""
        import inspect

        from conrod import pipeline

        source = inspect.getsource(pipeline.learn_taste)
        self.assertIn("rating_in_file", source)
        self.assertIn("COUNT(*)", source)


class _Session:
    """store.session() is a context manager; the tests hold one connection."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_exc):
        return False


if __name__ == "__main__":
    unittest.main()
