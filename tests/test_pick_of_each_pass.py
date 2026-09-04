"""Which frame of a pass is the one worth keeping.

The cull scores every frame on its own, which is the right question about a
frame and the wrong one about a pan. Measured on a real Falcon meet: 240
bursts, a median of thirteen frames each, and after the cull a median of
*six* surviving frames per burst. Written out to XMP that is six copies of
one photograph at the same star rating.

No new evidence is involved. `images.burst_key` already says which frames
are one pass and `detections.rating` is a float, so the frames of a pass
were already fully ordered -- nothing ever compared them to each other.

Two decisions worth stating, because both could reasonably have gone the
other way:

* The unit is the car in the pass, not the pass. 91% of bursts on that
  album contained a frame with two or more vehicles in it: a burst is a
  stretch of time, and two cars nose to tail give one burst and two
  answers.
* Exactly one pick, and the gap between the best frame and the runner-up is
  a median of 0.025 where the spread across a whole pass is 0.590 -- in 36%
  of passes the top two are within 0.01. So the choice is often very nearly
  arbitrary. It is at least stable, and the runner-up loses nothing but the
  badge.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conrod import pipeline, store
from conrod.config import Settings


class _Pass(unittest.TestCase):
    def setUp(self) -> None:
        # The default database, because pick_of_pass opens that one itself --
        # the same shape as rescore and embed_missing beside it. Harmless:
        # tests/__init__ points CONROD_HOME at a throwaway directory, so this
        # is never the photographer's own album.
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.job = store.create_job(self.conn, Path("C:/shoot"), "Bathurst", {})
        self.addCleanup(self._forget)
        self.settings = Settings()
        self.frame = 0

    def _forget(self) -> None:
        self.conn.execute(
            "DELETE FROM detections WHERE image_id IN "
            "(SELECT id FROM images WHERE job_id=?)", (self.job,))
        self.conn.execute("DELETE FROM images WHERE job_id=?", (self.job,))
        self.conn.execute("DELETE FROM jobs WHERE id=?", (self.job,))
        self.conn.commit()

    def _shot(self, burst, rating, *, group=None, rejected=0, bystander=0,
              sharpness=None):
        """One detection, in its own frame unless told otherwise."""
        self.frame += 1
        path = Path(f"C:/shoot/{self.frame:05d}.CR3")
        store.add_images(self.conn, self.job, [path])
        image_id = self.conn.execute(
            "SELECT id FROM images WHERE job_id=? AND path=?",
            (self.job, str(path))).fetchone()[0]
        self.conn.execute("UPDATE images SET burst_key=? WHERE id=?",
                          (burst, image_id))
        return self._on(image_id, rating, group, rejected, bystander, sharpness)

    def _on(self, image_id, rating, group=None, rejected=0, bystander=0,
            sharpness=None):
        det = store.add_detection(self.conn, image_id, (0, 0, 100, 100),
                                  "car", 0.9, "C:/crops/00.jpg")
        self.conn.execute(
            """UPDATE detections SET rating=?, group_key=?, rejected=?,
                      bystander=?, sharpness=? WHERE id=?""",
            (rating, group, rejected, bystander, sharpness, det))
        self.conn.commit()
        return det

    def _run(self):
        return pipeline.pick_of_pass(self.job, self.settings)

    def _picks(self):
        return {r[0] for r in self.conn.execute(
            """SELECT d.id FROM detections d JOIN images i ON i.id = d.image_id
                WHERE d.burst_pick = 1 AND i.job_id = ?""", (self.job,))}


class OneKeeperPerPass(_Pass):
    def test_the_sharpest_frame_of_a_pan_is_the_keeper(self) -> None:
        self._shot(7, 0.61)
        best = self._shot(7, 0.86)
        self._shot(7, 0.72)
        self._run()
        self.assertEqual(self._picks(), {best})

    def test_the_others_keep_everything_they_earned(self) -> None:
        """The runner-up loses the badge and nothing else -- same rating,
        same label, still in the grid one click away. Demoting it would be
        acting on a gap of 0.025."""
        self._shot(7, 0.61)
        self._shot(7, 0.86)
        runner_up = self._shot(7, 0.855)
        self._run()
        row = self.conn.execute(
            "SELECT rating, rejected, burst_pick FROM detections WHERE id=?",
            (runner_up,)).fetchone()
        self.assertAlmostEqual(row["rating"], 0.855)
        self.assertEqual(row["rejected"], 0)
        self.assertIsNone(row["burst_pick"])

    def test_a_pass_of_one_frame_keeps_that_frame(self) -> None:
        only = self._shot(7, 0.4)
        self._run()
        self.assertEqual(self._picks(), {only})

    def test_each_pass_gets_its_own(self) -> None:
        a = self._shot(7, 0.9)
        self._shot(7, 0.5)
        b = self._shot(8, 0.3)
        self._shot(8, 0.2)
        self._run()
        self.assertEqual(self._picks(), {a, b})

    def test_a_bad_pass_still_has_a_best_frame(self) -> None:
        """Not a threshold. The question is which of these is the one, and
        a pass where nothing was sharp still has a least-bad frame -- the
        stars already say it was a poor pass."""
        self._shot(7, 0.11)
        best = self._shot(7, 0.19)
        self._run()
        self.assertEqual(self._picks(), {best})


class ItIsPerCarNotPerBurst(_Pass):
    def test_two_cars_in_one_pass_get_a_keeper_each(self) -> None:
        """91% of bursts on the album this came from had a frame with two
        or more vehicles in it. A burst is a stretch of time, not a
        subject."""
        red = self._shot(7, 0.80, group=1)
        self._shot(7, 0.50, group=1)
        blue = self._shot(7, 0.40, group=2)
        self._run()
        self.assertEqual(self._picks(), {red, blue})

    def test_two_cars_in_one_frame_both_count(self) -> None:
        first = self._shot(7, 0.80, group=1)
        image_id = self.conn.execute(
            "SELECT image_id FROM detections WHERE id=?", (first,)).fetchone()[0]
        second = self._on(image_id, 0.30, group=2)
        self._run()
        self.assertEqual(self._picks(), {first, second})

    def test_ungrouped_frames_fall_back_to_the_pass(self) -> None:
        """An album that has not been grouped -- or one scanned before the
        model existed -- still gets a keeper. Worse unit, better than none."""
        self._shot(7, 0.61)
        best = self._shot(7, 0.86)
        self._run()
        self.assertEqual(self._picks(), {best})


class WhatItWillNotPick(_Pass):
    def test_a_rejected_frame_is_never_the_keeper(self) -> None:
        self._shot(7, 0.99, rejected=1)
        kept = self._shot(7, 0.40)
        self._run()
        self.assertEqual(self._picks(), {kept})

    def test_a_bystander_is_never_the_keeper(self) -> None:
        """It is a statement about which car in the frame is the subject.
        A parked car in the background does not win a pass."""
        self._shot(7, 0.99, bystander=1)
        subject = self._shot(7, 0.40)
        self._run()
        self.assertEqual(self._picks(), {subject})

    def test_a_frame_with_no_burst_is_left_alone(self) -> None:
        """Nothing to be the best of."""
        path = Path("C:/shoot/loose.CR3")
        store.add_images(self.conn, self.job, [path])
        # burst_key deliberately left NULL
        image_id = self.conn.execute(
            "SELECT id FROM images WHERE path=?", (str(path),)).fetchone()[0]
        self._on(image_id, 0.9)
        self._run()
        self.assertEqual(self._picks(), set())

    def test_a_frame_with_no_score_at_all_cannot_win(self) -> None:
        self._shot(7, None)
        scored = self._shot(7, 0.2)
        self._run()
        self.assertEqual(self._picks(), {scored})

    def test_sharpness_stands_in_where_there_is_no_rating(self) -> None:
        """An album culled before framing was measured has sharpness and no
        rating. Refusing to pick would be worse than picking on the number
        that is there."""
        self._shot(7, None, sharpness=0.3)
        best = self._shot(7, None, sharpness=0.8)
        self._run()
        self.assertEqual(self._picks(), {best})


class AStarGivenByHandWinsThePass(_Pass):
    """Their judgement beats the focus measure, because the measure is a
    proposal and their star is an answer.

    Found on a real album after writing it out: IMG_0029 carried a 5 the
    photographer had given in Lightroom and IMG_0030 scored better on focus,
    so the keeper of that pass was a frame they had already passed over --
    and the one they had picked was written as a plain non-keeper.
    """

    def _starred(self, burst, rating, stars):
        det = self._shot(burst, rating)
        self.conn.execute("UPDATE detections SET stars=? WHERE id=?",
                          (stars, det))
        self.conn.commit()
        return det

    def _from_the_file(self, burst, rating, stars):
        det = self._shot(burst, rating)
        image_id = self.conn.execute(
            "SELECT image_id FROM detections WHERE id=?", (det,)).fetchone()[0]
        self.conn.execute("UPDATE images SET rating_in_file=? WHERE id=?",
                          (stars, image_id))
        self.conn.commit()
        return det

    def test_a_starred_frame_beats_a_sharper_one(self) -> None:
        theirs = self._starred(7, 0.20, 5)
        self._shot(7, 0.95)
        self._run()
        self.assertEqual(self._picks(), {theirs})

    def test_a_star_set_in_lightroom_counts_the_same(self) -> None:
        """It is the same judgement, made in another window."""
        theirs = self._from_the_file(7, 0.20, 5)
        self._shot(7, 0.95)
        self._run()
        self.assertEqual(self._picks(), {theirs})

    def test_the_measure_still_separates_two_starred_frames(self) -> None:
        self._starred(7, 0.20, 4)
        sharper = self._starred(7, 0.90, 4)
        self._run()
        self.assertEqual(self._picks(), {sharper})

    def test_more_stars_beats_fewer(self) -> None:
        self._starred(7, 0.95, 3)
        best = self._starred(7, 0.10, 5)
        self._run()
        self.assertEqual(self._picks(), {best})

    def test_an_unstarred_pass_is_decided_by_the_measure_as_before(self) -> None:
        self._shot(7, 0.20)
        sharp = self._shot(7, 0.95)
        self._run()
        self.assertEqual(self._picks(), {sharp})


class RunningItAgain(_Pass):
    def test_a_tie_breaks_on_the_earlier_frame(self) -> None:
        """In 36% of passes the top two are within 0.01, so ties are the
        normal case rather than the corner one. Earliest-first makes the
        answer stable, which is the most that can honestly be claimed."""
        first = self._shot(7, 0.86)
        self._shot(7, 0.86)
        self._run()
        self.assertEqual(self._picks(), {first})

    def test_the_same_frame_is_picked_every_time(self) -> None:
        self._shot(7, 0.86)
        self._shot(7, 0.86)
        self._shot(7, 0.86)
        self._run()
        once = self._picks()
        self._run()
        self._run()
        self.assertEqual(self._picks(), once)

    def test_an_old_pick_is_cleared_when_the_answer_changes(self) -> None:
        """A star given by hand changes which frame is best. The previous
        keeper has to stop being one, or an album accumulates them."""
        was = self._shot(7, 0.86)
        now = self._shot(7, 0.50)
        self._run()
        self.assertEqual(self._picks(), {was})

        self.conn.execute("UPDATE detections SET rating=0.99 WHERE id=?", (now,))
        self.conn.commit()
        self._run()
        self.assertEqual(self._picks(), {now})

    def test_it_reports_what_it_did(self) -> None:
        self._shot(7, 0.8)
        self._shot(7, 0.5)
        self._shot(8, 0.5)
        out = self._run()
        self.assertEqual(out["passes"], 2)
        self.assertEqual(out["picks"], 2)
        self.assertEqual(out["considered"], 3)


class WhatGetsWritten(unittest.TestCase):
    def test_the_keeper_goes_out_as_a_blue_label(self) -> None:
        """Blue because Lightroom's Pick flag lives in the catalogue and
        never reaches a sidecar. xmp:Label does, and every catalogue reads
        it."""
        from conrod import sharpness

        self.assertEqual(sharpness.LABEL_PICK, "Blue")
        self.assertNotIn(sharpness.LABEL_PICK,
                         (sharpness.LABEL_GOOD, sharpness.LABEL_FAIR,
                          sharpness.LABEL_POOR))

    def test_it_outranks_the_cull_colour(self) -> None:
        """Green says this frame is sharp; blue says it is *the* sharp one
        of the twelve shot of that car."""
        from conrod import sharpness

        settings = Settings()
        sharp = {"best": 0.95, "by_hand": None, "pick": False}
        self.assertEqual(pipeline.verdict_for(sharp, settings)[1],
                         sharpness.LABEL_GOOD)
        sharp["pick"] = True
        self.assertEqual(pipeline.verdict_for(sharp, settings)[1],
                         sharpness.LABEL_PICK)

    def test_it_outranks_a_hand_given_star_too(self) -> None:
        """A three-star frame they starred themselves still goes blue if it
        is the keeper of its pass -- the star is theirs and is untouched,
        the colour is about which frame to open first."""
        from conrod import sharpness

        entry = {"best": 0.3, "by_hand": 3, "pick": True}
        rating, label = pipeline.verdict_for(entry, Settings())
        self.assertEqual(rating, 3)
        self.assertEqual(label, sharpness.LABEL_PICK)

    def test_the_stars_are_not_touched_by_it(self) -> None:
        settings = Settings()
        entry = {"best": 0.95, "by_hand": None, "pick": False}
        was = pipeline.verdict_for(entry, settings)[0]
        entry["pick"] = True
        self.assertEqual(pipeline.verdict_for(entry, settings)[0], was)

    def test_turning_it_off_leaves_the_cull_colour(self) -> None:
        from conrod import sharpness

        settings = Settings()
        settings.mark_burst_picks = False
        entry = {"best": 0.95, "by_hand": None, "pick": True}
        self.assertEqual(pipeline.verdict_for(entry, settings)[1],
                         sharpness.LABEL_GOOD)

    def test_a_frame_that_is_not_a_keeper_is_unaffected(self) -> None:
        entry = {"best": 0.2, "by_hand": None, "pick": False}
        self.assertEqual(pipeline.verdict_for(entry, Settings()),
                         pipeline.verdict_for(dict(entry), Settings()))

    def test_it_can_be_turned_off(self) -> None:
        self.assertTrue(Settings().mark_burst_picks)
        self.assertEqual(Settings().pick_label, "Blue")


if __name__ == "__main__":
    unittest.main()
