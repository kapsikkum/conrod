"""One pile per car, decided by what the crops look like.

The signals grouping used to run on were measured on a real burst and found
wanting: a difference hash ran 2..32 bits for the same car and 9..40 for
different ones, and the colour histogram scored about 1.00 for every pair
including a green ute against a blue hatchback. So the vision model's guess
at the make ended up doing the gatekeeping -- which made grouping depend on
the thing grouping exists to correct.

An embedding answers the question directly. Measured on 45 bursts of a real
shoot: the same car runs a median 0.951, different cars a median 0.450 with a
99th percentile of 0.869.

No model is called here. These tests use vectors written by hand, because the
rules being checked are about what the numbers mean and not about the network.
"""

from __future__ import annotations

import unittest

import numpy as np

from conrod import grouping


def _vec(*values) -> np.ndarray:
    """A unit vector, so a dot product is a cosine."""
    v = np.array(values, dtype=np.float32)
    return v / np.linalg.norm(v)


def _like(base: np.ndarray, nearness: float) -> np.ndarray:
    """A vector a known cosine away from another."""
    other = np.zeros_like(base)
    other[-1] = 1.0
    other = other - float(np.dot(other, base)) * base
    other = other / np.linalg.norm(other)
    return base * nearness + other * float(np.sqrt(1 - nearness ** 2))


CAR = _vec(1, 0, 0, 0, 0)


class WithinABurst(unittest.TestCase):
    def test_crops_that_look_alike_are_one_car(self) -> None:
        rows = [(1, CAR, 1, 7, None), (2, _like(CAR, 0.97), 2, 7, None)]
        out = grouping.cluster_by_look(rows)
        self.assertEqual(out[1], out[2])

    def test_crops_that_do_not_are_two_cars(self) -> None:
        rows = [(1, CAR, 1, 7, None), (2, _like(CAR, 0.40), 2, 7, None)]
        out = grouping.cluster_by_look(rows)
        self.assertNotEqual(out[1], out[2])

    def test_the_threshold_is_where_it_was_measured(self) -> None:
        """Different cars reach 0.869 at the 99th percentile, so 0.86 is
        inside the overlap and 0.90 is not."""
        self.assertGreaterEqual(grouping.SAME_CAR, 0.87)

    def test_a_car_cannot_be_in_one_photograph_twice(self) -> None:
        """Two detections in the same frame are two vehicles however alike.

        A pack shot of identical race cars is the case: they genuinely look
        the same, and they are still not the same car.
        """
        rows = [(1, CAR, 5, 7, None), (2, _like(CAR, 0.999), 5, 7, None)]
        out = grouping.cluster_by_look(rows)
        self.assertNotEqual(out[1], out[2])

    def test_a_group_is_matched_on_all_of_it_not_just_the_first_frame(self) -> None:
        """A car turning through a corner drifts away from its own first
        frame, while always resembling the frame before it."""
        a, b, c = CAR, _like(CAR, 0.95), None
        c = _like(b, 0.95)
        rows = [(1, a, 1, 7, None), (2, b, 2, 7, None), (3, c, 3, 7, None)]
        out = grouping.cluster_by_look(rows)
        self.assertEqual(out[1], out[2])
        self.assertEqual(out[2], out[3])


class AcrossBursts(unittest.TestCase):
    """Only a plate joins two bursts.

    Deliberately not resemblance: two silver hatchbacks on different passes
    look far more alike than one car does from the front and from behind, so
    similarity across bursts merges the wrong things confidently.
    """

    def test_looking_alike_is_not_enough(self) -> None:
        rows = [(1, CAR, 1, 7, None), (2, _like(CAR, 0.99), 2, 9, None)]
        out = grouping.cluster_by_look(rows)
        self.assertNotEqual(out[1], out[2])

    def test_the_same_plate_joins_them(self) -> None:
        rows = [(1, CAR, 1, 7, "39432J"), (2, _like(CAR, 0.10), 2, 9, "39432J")]
        out = grouping.cluster_by_look(rows)
        self.assertEqual(out[1], out[2])

    def test_a_misread_plate_still_joins_them(self) -> None:
        """43111J in one frame and 73111J in the next is one plate.

        Because a plate is identity, a mismatch is proof of a *different*
        vehicle -- so taking a misread literally split one car panned across
        twenty-seven frames into four, and the eleven frames that read it
        correctly never got to outvote the sixteen that did not. The match
        therefore allows one character, and only the substitutions a reader
        actually makes on a plate thirty pixels wide and half motion blur.
        """
        rows = [(1, CAR, 1, 7, "43111J"), (2, _like(CAR, 0.10), 2, 9, "73111J")]
        out = grouping.cluster_by_look(rows)
        self.assertEqual(out[1], out[2])

    def test_a_plate_two_characters_out_is_a_different_car(self) -> None:
        """The allowance is one character. Two is not a misread, it is
        another plate, and a plate is an identity."""
        rows = [(1, CAR, 1, 7, "43111J"), (2, _like(CAR, 0.10), 2, 9, "73118J")]
        out = grouping.cluster_by_look(rows)
        self.assertNotEqual(out[1], out[2])

    def test_different_plates_stay_apart(self) -> None:
        rows = [(1, CAR, 1, 7, "ABC123"), (2, _like(CAR, 0.99), 2, 9, "XYZ789")]
        out = grouping.cluster_by_look(rows)
        self.assertNotEqual(out[1], out[2])

    def test_different_plates_in_one_burst_are_two_cars(self) -> None:
        """A plate is an identity, and it outranks looking alike."""
        rows = [(1, CAR, 1, 7, "ABC123"), (2, _like(CAR, 0.99), 2, 7, "XYZ789")]
        out = grouping.cluster_by_look(rows)
        self.assertNotEqual(out[1], out[2])


class WhatItDoesNotUse(unittest.TestCase):
    def test_no_vision_model_is_consulted(self) -> None:
        """Pressing Group cars used to be able to sit for an hour behind a
        rate limit, answering a question the crops already answer."""
        import inspect as inspect_mod

        source = inspect_mod.getsource(grouping.cluster_by_look)
        for banned in ("vlm", "identify_burst", "normalise", "make"):
            self.assertNotIn(banned, source.replace("# ", "").split('"""')[-1])

    def test_a_crop_with_no_embedding_is_left_alone(self) -> None:
        """Better ungrouped than grouped on nothing."""
        rows = [(1, CAR, 1, 7, None), (2, None, 2, 7, None)]
        out = grouping.cluster_by_look(rows)
        self.assertIn(1, out)
        self.assertNotIn(2, out)


if __name__ == "__main__":
    unittest.main()
