"""Culling by hand: reject instantly, rate with the number keys, sort by star.

The assisted cull measures sharpness and framing and is right most of the
time. This is for the rest of it, and for the judgements no measurement
makes — the frame where the light is doing something, the one where the
driver is looking at the camera. Going through a shoot at a card a second
means the keyboard has to be able to do the whole job.

A rating given by hand is the answer and the measured one is a proposal, so
it wins everywhere: on the card, in the sort order, and in what Write XMP
puts in the file. It lives in its own column so that re-culling an album
cannot quietly erase the photographer's own pass.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from conrod import server, sharpness


class TheStarColumn(unittest.TestCase):
    def test_a_hand_rating_survives_another_cull(self) -> None:
        """The reason it is not written over `rating`. Re-culling an album
        recomputes every measured rating, and it must not take an
        afternoon's judgements with it."""
        from conrod import store

        self.assertIn(("detections", "stars", "INTEGER"), store._MIGRATIONS)

    def test_zero_means_forget_what_i_said(self) -> None:
        """Not "zero stars" -- every catalogue reads 0 as unrated, and there
        has to be a way back to the measured rating after a mis-key."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("stars = body.stars or None", source)

    def test_clearing_a_rating_reports_the_measured_one_back(self) -> None:
        """Found by pressing 0 in the browser: the card fell to "-" on a
        frame the cull had rated three stars. The endpoint was returning the
        manual column, which is now empty, rather than what the card should
        show -- so the pill disagreed with every other view of the same
        vehicle until the grid was reloaded."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        body = source[source.index("def update_detection"):]
        self.assertIn('"stars": stars or (None if row["rating"] is None', body)
        self.assertIn('"by_hand": stars is not None', body)

    def test_the_api_refuses_a_rating_off_the_scale(self) -> None:
        from conrod.server import DetectionUpdate
        from pydantic import ValidationError

        self.assertEqual(DetectionUpdate(stars=5).stars, 5)
        self.assertEqual(DetectionUpdate(stars=0).stars, 0)
        for bad in (6, -1, 99):
            with self.assertRaises(ValidationError):
                DetectionUpdate(stars=bad)


class SortingByStar(unittest.TestCase):
    """The ordering runs in SQLite, so the bands are written out in SQL.

    Generated from STAR_BANDS rather than typed again, because a sort that
    orders by numbers nothing else in the app agrees with is worse than no
    sort at all.
    """

    def _album(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE detections (id INTEGER PRIMARY KEY,"
                     " rating REAL, stars INTEGER)")
        conn.executemany("INSERT INTO detections VALUES (?,?,?)", rows)
        return conn

    def _order(self, conn, how):
        # Aliased `d`, because the orderings are written against the join in
        # DETECTION_QUERY. Shooting order also mentions the images table,
        # which is not what these tests are about.
        sql = (f"SELECT d.id AS id FROM detections d"
               f" ORDER BY {server.ORDERINGS[how]}".replace("i.id ASC, ", ""))
        return [r["id"] for r in conn.execute(sql)]

    def test_the_sql_bands_match_the_ones_the_app_uses(self) -> None:
        conn = self._album([(n, floor, None) for n, (floor, _)
                            in enumerate(sharpness.STAR_BANDS, start=1)])
        rank = server._RANK.replace("d.", "")
        for row in conn.execute(f"SELECT id, rating, {rank} AS stars"
                                " FROM detections"):
            self.assertEqual(row["stars"], sharpness.stars_for(row["rating"]),
                             f"SQL and stars_for disagree at {row['rating']}")

    def test_best_first_puts_the_sharpest_at_the_top(self) -> None:
        conn = self._album([(1, 0.20, None), (2, 0.90, None), (3, 0.55, None)])
        self.assertEqual(self._order(conn, "best")[0], 2)
        self.assertEqual(self._order(conn, "worst")[0], 1)

    def test_a_hand_rating_decides_where_a_frame_sorts(self) -> None:
        """A soft frame the photographer starred is a keeper, and burying it
        under the sharp ones defeats the point of having rated it."""
        conn = self._album([(1, 0.95, None), (2, 0.10, 5)])
        self.assertEqual(self._order(conn, "best")[0], 2)

    def test_unrated_frames_go_last_in_both_directions(self) -> None:
        """An unrated frame is not a bad one. Sorting worst-first must not
        bury the ones nothing has looked at yet under the rejects."""
        conn = self._album([(1, None, None), (2, 0.90, None), (3, 0.10, None)])
        self.assertEqual(self._order(conn, "best")[-1], 1)
        self.assertEqual(self._order(conn, "worst")[-1], 1)

    def test_the_endpoint_only_accepts_orderings_that_exist(self) -> None:
        """The name goes into an ORDER BY clause, so it may never be free
        text."""
        import inspect

        signature = inspect.signature(server.detections)
        query = signature.parameters["sort"].default
        # FastAPI keeps the constraint in pydantic metadata, not on the Query.
        pattern = next(m.pattern for m in query.metadata
                       if getattr(m, "pattern", None))
        self.assertEqual(set(server.ORDERINGS),
                         set(pattern.strip("^$()").split("|")))


class WritingWhatWasChosen(unittest.TestCase):
    """Write XMP has to write the rating the person gave, not the measured
    one, or rating a shoot by hand achieves nothing outside Conrod."""

    def test_the_writer_prefers_the_hand_rating(self) -> None:
        source = Path(
            __import__("conrod.pipeline", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        body = source[source.index("def write_job"):]
        self.assertIn('entry["by_hand"]', body)
        # And it is taken per frame the same way the measured one is: the
        # best vehicle in the photograph speaks for the photograph.
        self.assertIn('row["stars"] > entry["by_hand"]', body)

    def test_the_colour_follows_the_stars_that_were_given(self) -> None:
        """A frame starred in review must not stay red in the catalogue."""
        self.assertEqual(sharpness.label_for("good"), "Green")
        self.assertEqual(sharpness.label_for("poor"), "Red")
        source = Path(
            __import__("conrod.pipeline", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        self.assertIn("label = sharpness_mod.label_for(", source)


class TheKeyboard(unittest.TestCase):
    """Checked against the source: a shortcut that quietly stops being wired
    up is invisible until someone tries it mid-shoot."""

    def setUp(self):
        import conrod

        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")

    def test_every_advertised_shortcut_is_handled(self) -> None:
        for key in ('"j"', '"k"', '"x"', '"u"', '"Enter"', '"Delete"',
                    '"ArrowRight"', '"ArrowLeft"'):
            self.assertIn(key, self.code, f"{key} is advertised but not bound")

    def test_typing_a_number_is_not_treated_as_a_shortcut(self) -> None:
        """The number keys rate a frame, and the same keys type a competition
        number into the box on the card. Losing that would make the grid
        uneditable."""
        self.assertIn('tag === "input"', self.code)

    def test_rejecting_does_not_ask_for_confirmation(self) -> None:
        """"Instantly" was the whole request. Nothing is written to the
        photograph either way, and U puts it back."""
        cut = self.code[self.code.index("async function cutCard"):][:400]
        self.assertNotIn("confirm(", cut)


if __name__ == "__main__":
    unittest.main()
