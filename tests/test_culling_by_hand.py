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

    def test_a_judgement_outranks_a_calculation_of_the_same_number(self) -> None:
        """Once the measure could reach five as well, a frame someone
        starred tied with a sharp one that had merely been calculated to the
        same value -- and lost the tiebreak on raw sharpness."""
        conn = self._album([(1, 0.99, None), (2, 0.40, 5)])
        self.assertEqual(sharpness.stars_for(0.99), 5)
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


class FilteringByStar(unittest.TestCase):
    """"3 stars and up" as a filter, the way Aftershoot's toolbar has it --
    not only a sort order to scroll through until the rating drops off.
    Built on the same effective-stars expression the sort already uses, so
    a card showing 4 stars and a filter set to 3+ can never disagree about
    whether it belongs."""

    def _album(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE detections (id INTEGER PRIMARY KEY,"
                     " rating REAL, stars INTEGER)")
        conn.executemany("INSERT INTO detections VALUES (?,?,?)", rows)
        return conn

    def _at_least(self, conn, n):
        rank = server._RANK.replace("d.", "")
        sql = f"SELECT id FROM detections d WHERE ({rank}) >= ? ORDER BY id"
        return [r["id"] for r in conn.execute(sql, (n,))]

    def test_only_frames_at_or_above_the_floor_pass(self) -> None:
        conn = self._album([(1, None, 2), (2, None, 3), (3, None, 5)])
        self.assertEqual(self._at_least(conn, 3), [2, 3])

    def test_a_measured_rating_counts_the_same_as_a_hand_rating(self) -> None:
        """The filter reads the same effective-stars value the card shows
        and the sort uses -- a frame the cull rated highly must not be
        invisible to a high floor just because nobody starred it by hand."""
        conn = self._album([(n, floor, None) for n, (floor, _)
                            in enumerate(sharpness.STAR_BANDS, start=1)])
        top_stars = max(stars for _, stars in sharpness.STAR_BANDS)
        top_row = next(n for n, (_, stars) in
                       enumerate(sharpness.STAR_BANDS, start=1) if stars == top_stars)
        self.assertIn(top_row, self._at_least(conn, top_stars))

    def test_unrated_frames_never_pass_any_floor(self) -> None:
        conn = self._album([(1, None, None)])
        for n in range(1, 6):
            self.assertEqual(self._at_least(conn, n), [])

    def test_the_endpoint_rejects_a_floor_off_the_scale(self) -> None:
        import inspect

        signature = inspect.signature(server.detections)
        query = signature.parameters["min_stars"].default
        bounds = {getattr(m, "ge", None): True for m in query.metadata}
        self.assertIn(1, bounds)
        bounds = {getattr(m, "le", None): True for m in query.metadata}
        self.assertIn(5, bounds)


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


class TheFullFrameDialog(unittest.TestCase):
    """The zoomed-in view is where a soft or borderline frame actually gets
    judged, so a person had to close it and go hunting for the small card
    behind it just to cull the thing they were already looking at."""

    def setUp(self):
        import conrod

        self.code = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        self.html = (Path(conrod.__file__).parent / "web" / "index.html").read_text(
            encoding="utf-8")

    def test_the_dialog_carries_reject_and_star_controls(self) -> None:
        self.assertIn('id="frame-reject"', self.html)
        self.assertIn('id="frame-stars"', self.html)

    def test_opening_a_frame_remembers_which_card_it_came_from(self) -> None:
        """Both views draw off the same card node, through the same
        setStars/cutCard, so the dialog and the grid behind it can never
        disagree about a vehicle's state."""
        opening = self.code[self.code.index("function openFrame(node)"):][:400]
        self.assertIn("state.dialogNode = node", opening)

    def test_the_arrows_walk_the_vehicles_other_frames(self) -> None:
        """Culling a twenty-five frame burst used to mean open, judge,
        close, click the next, twenty-five times over."""
        self.assertIn("function stepFrame(by)", self.code)
        handler = self.code[self.code.index('document.addEventListener("keydown"'):]
        self.assertIn("stepFrame(1)", handler[:2500])
        self.assertIn("stepFrame(-1)", handler[:2500])

    def test_it_says_where_you_are_in_the_burst(self) -> None:
        render = self.code[self.code.index("function renderFrame()"):]
        self.assertIn("of ${all.length}", render)

    def test_star_and_reject_buttons_call_the_one_true_functions(self) -> None:
        body = self.code[self.code.index("function renderFrame()"):][:3500]
        self.assertIn("setStars(node", body)
        self.assertIn("cutCard(node", body)

    def test_jk_do_not_move_the_cursor_behind_a_dialog_the_person_cannot_see(
        self,
    ) -> None:
        """j/k used to move state.cursor regardless of what was on screen --
        harmless in the grid, but from inside the dialog it meant the next X
        could cull a vehicle nobody was looking at."""
        handler = self.code[self.code.index('document.addEventListener("keydown"'):]
        self.assertIn("!dialogOpen && (key", handler[:2000])

    def test_closing_the_dialog_forgets_the_card(self) -> None:
        """Every way out -- the button, the backdrop, Escape -- has to clear
        it, or a stale reference could let a keystroke reach a card that is
        no longer the one on screen."""
        self.assertIn('addEventListener("close", () => { state.dialogNode = null; })',
                      self.code)


if __name__ == "__main__":
    unittest.main()
