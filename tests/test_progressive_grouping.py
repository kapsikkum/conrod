"""Grouping used to appear exactly once, after the very last frame of an
album had been identified. On a multi-thousand-frame shoot that meant a
wall of ungrouped single-frame cards for the whole scan -- correct, but it
read as if grouping were an afterthought rather than something the album
was building towards the whole time.

`_maybe_regroup` re-runs `grouping.consolidate()` every so often while a
scan is still going, so vehicles start forming before the scan finishes.
It changes nothing about grouping's own accuracy -- consolidate() is
documented as safe to call any number of times -- this is purely about
when the first useful call happens.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from conrod import grouping, pipeline
from conrod.config import Settings


class TheCheckpoint(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.counters = {"analysed": 0, "identified": 0}
        self.lock = threading.Lock()
        self.checkpoint = {"last": 0}
        self.errors: list[str] = []
        self.events: list[dict] = []

    def _tick(self, analysed: int, *, calls) -> None:
        self.counters["analysed"] = analysed
        with patch.object(pipeline.grouping, "consolidate", side_effect=calls) as mocked:
            pipeline._maybe_regroup(None, 1, self.settings, self.counters,
                                    self.lock, self.checkpoint, self.errors,
                                    self.events.append)
        return mocked

    def test_does_not_fire_before_the_threshold(self) -> None:
        mocked = self._tick(pipeline.REGROUP_CHECKPOINT - 1, calls=None)
        mocked.assert_not_called()
        self.assertEqual(self.events, [])

    def test_fires_once_the_threshold_is_crossed(self) -> None:
        mocked = self._tick(pipeline.REGROUP_CHECKPOINT,
                            calls=[{"vehicles": 4, "groups": 2}])
        mocked.assert_called_once()
        self.assertEqual(self.events[-1]["stage"], "grouping")

    def test_does_not_fire_again_until_another_threshold_worth_has_passed(self) -> None:
        self._tick(pipeline.REGROUP_CHECKPOINT, calls=[{"vehicles": 1, "groups": 1}])
        mocked = self._tick(pipeline.REGROUP_CHECKPOINT + 1, calls=None)
        mocked.assert_not_called()
        mocked = self._tick(2 * pipeline.REGROUP_CHECKPOINT,
                            calls=[{"vehicles": 2, "groups": 1}])
        mocked.assert_called_once()

    def test_disabled_grouping_never_regroups_mid_scan(self) -> None:
        self.settings.group_vehicles = False
        mocked = self._tick(pipeline.REGROUP_CHECKPOINT * 5, calls=None)
        mocked.assert_not_called()
        self.assertEqual(self.events, [])

    def test_a_failed_regroup_is_recorded_but_does_not_raise(self) -> None:
        """The final regroup at the end of a scan already tolerates this --
        a checkpoint mid-scan must not be the one place a scan can die."""
        self._tick(pipeline.REGROUP_CHECKPOINT, calls=RuntimeError("boom"))
        self.assertTrue(any("grouping" in e for e in self.errors))


if __name__ == "__main__":
    unittest.main()


class TheOwnReadingSnapshot(unittest.TestCase):
    """What each frame's own reader said, kept safe from the group's answer.

    Grouping records own_make before it overwrites make, so a bad merge can
    be undone. Once grouping started running at checkpoints *during* a scan
    rather than only at the end, the first checkpoint fired before anything
    had been identified -- and a plain setdefault froze own_make=None onto
    every detection in the album. The key then existed, so no later pass
    replaced it, and every regroup afterwards voted on blanks: 46 frames
    that all said Kawasaki Ninja ZX-6R reported nothing agreed.
    """

    def test_a_blank_is_not_recorded_as_a_reading(self) -> None:
        """The snapshot has to wait until there is something to snapshot."""
        current = {"make": None, "model": None, "colour": None}
        grouping.remember_own_reading(current)
        self.assertNotIn("own_make", current)

        current["make"] = "Kawasaki"
        grouping.remember_own_reading(current)
        self.assertEqual(current["own_make"], "Kawasaki")

    def test_a_real_reading_is_never_overwritten(self) -> None:
        """The whole point of the snapshot: the group must not eat it."""
        current = {"make": "Hyundai", "own_make": "Hyundai"}
        current["make"] = "Kawasaki"          # a bad merge writes its answer
        grouping.remember_own_reading(current)
        self.assertEqual(current["own_make"], "Hyundai")

    def test_a_group_that_all_says_the_same_thing_agrees_completely(self) -> None:
        members = [{"make": "Kawasaki", "model": "Ninja ZX-6R"} for _ in range(46)]
        self.assertEqual(grouping.consensus(members).agreement, 1.0)

    def test_a_poisoned_blank_does_not_silence_a_named_frame(self) -> None:
        """Albums written before the fix carry own_make=None on every row.

        Reading that back as the frame's opinion is what has to stop, or the
        shoot stays stuck at nothing-agreed even once it is identified.
        """
        parsed = {"make": "Kawasaki", "own_make": None}
        grouping.use_own_reading(parsed)
        self.assertEqual(parsed["make"], "Kawasaki")

    def test_a_real_own_reading_still_wins_over_a_group_answer(self) -> None:
        """The protection this was built for has to survive the fix."""
        parsed = {"make": "Kawasaki", "own_make": "Hyundai"}
        grouping.use_own_reading(parsed)
        self.assertEqual(parsed["make"], "Hyundai")
