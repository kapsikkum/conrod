"""The "time left" estimate on the scan screen.

A 6,000-frame scan announced "about 86.2 h left" from its first four frames,
because the estimate divided by the time since the scan began -- which counted
the minutes spent enumerating, culling and extracting previews as though
frames had been analysed during them.
"""

from __future__ import annotations

import time
import unittest

from conrod import server


class Eta(unittest.TestCase):
    def _feed(self, samples, total, done=None):
        server._rate.clear()
        now = time.time()
        for offset, count in samples:
            server._rate.append((now + offset, count))
        server._run.update({"active": True, "total": total,
                            "done": done if done is not None else samples[-1][1]})

    def test_it_says_nothing_until_there_is_evidence(self):
        # Three minutes of setup, then four frames. This is the real case.
        self._feed([(-180, 0), (-26, 0), (-18, 1), (-11, 2), (-4, 3), (0, 4)],
                   total=6211)
        self.assertIsNone(server._estimate_eta())

    def test_a_steady_rate_gives_the_right_answer(self):
        # 10s a frame, 24 frames done, 6211 total -> a bit over 17 hours.
        self._feed([(-240 + i * 10, i) for i in range(25)], total=6211)
        eta = server._estimate_eta()
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta / 3600, 17.2, delta=1.0)

    def test_it_follows_a_run_that_speeds_up(self):
        # Twenty slow frames then a hundred fast ones. Averaging the whole run
        # would still be reporting the slow pace.
        slow = [(-600 + i * 20, i) for i in range(20)]
        fast = [(-200 + i * 2, 20 + i) for i in range(100)]
        self._feed(slow + fast, total=1000)
        eta = server._estimate_eta()
        self.assertIsNotNone(eta)
        self.assertLess(eta, 3600)          # minutes, not hours

    def test_a_finished_scan_has_nothing_left(self):
        self._feed([(-100 + i, i) for i in range(101)], total=100, done=100)
        self.assertEqual(server._estimate_eta(), 0)

    def test_the_estimate_does_not_carry_over_between_scans(self):
        self._feed([(-240 + i * 10, i) for i in range(25)], total=6211)
        self.assertIsNotNone(server._estimate_eta())
        server._rate.clear()
        self.assertIsNone(server._estimate_eta())


if __name__ == "__main__":
    unittest.main()
