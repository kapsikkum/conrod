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

from conrod import pipeline
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
