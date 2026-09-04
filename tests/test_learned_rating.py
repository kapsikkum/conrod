"""Learning the photographer's scale, and knowing when not to.

The sharpness measure agreed with one photographer's 944 ratings exactly 60%
of the time and within one star 89%. Fitting their own ratings instead takes
that to 65% and 97%, cross-validated on frames the fit never saw.

The rules that matter are about deference and about restraint: a prediction
must never overrule an answer, and with too little to learn from there must
be no model at all rather than a confident one fitted to noise.
"""

from __future__ import annotations

import unittest

import numpy as np

from conrod import taste


def _ratings(n=400, seed=0):
    """Crops whose direction carries the quality, plus noise.

    Direction, not magnitude, and the distinction is the whole fixture: these
    are unit vectors, so anything encoded as "how long is it" is divided away
    by the normalisation and the model is handed noise. Written the wrong way
    round first, which looked like the fit failing.
    """
    rng = np.random.default_rng(seed)
    vectors, stars = [], []
    for _ in range(n):
        star = int(rng.integers(1, 6))
        angle = (star - 1) / 4.0 * (np.pi / 2)      # 1..5 sweeps e0 -> e1
        v = rng.normal(0, 0.05, 8)
        v[0] += np.cos(angle)
        v[1] += np.sin(angle)
        vectors.append(v / np.linalg.norm(v))
        stars.append(star)
    return vectors, stars


class LearningIt(unittest.TestCase):
    def test_it_learns_a_scale_that_is_actually_there(self) -> None:
        vectors, stars = _ratings()
        model = taste.fit(vectors, stars)
        self.assertIsNotNone(model)
        guesses = [taste.predict(model, v) for v in vectors]
        self.assertGreater(sum(g == s for g, s in zip(guesses, stars)) / len(stars),
                           0.7)

    def test_it_reports_agreement_on_frames_it_did_not_see(self) -> None:
        """Fitting and scoring the same frames reports something flattering
        and meaningless."""
        vectors, stars = _ratings()
        score = taste.agreement(vectors, stars)
        self.assertIsNotNone(score)
        self.assertGreater(score["within_one"], 0.9)
        self.assertEqual(score["n"], len(stars))


class KnowingWhenNotTo(unittest.TestCase):
    def test_too_few_ratings_means_no_model(self) -> None:
        """384 inputs and a handful of frames reproduces them exactly and
        predicts nothing. Better to fall back to measuring focus."""
        vectors, stars = _ratings(n=20)
        self.assertIsNone(taste.fit(vectors, stars))

    def test_ratings_that_are_all_the_same_teach_nothing(self) -> None:
        vectors, _ = _ratings()
        self.assertIsNone(taste.fit(vectors, [3] * len(vectors)))

    def test_no_agreement_figure_without_enough_to_learn_from(self) -> None:
        vectors, stars = _ratings(n=20)
        self.assertIsNone(taste.agreement(vectors, stars))


class StayingOnTheScale(unittest.TestCase):
    def test_a_prediction_is_always_a_star_between_one_and_five(self) -> None:
        """A regression will happily predict 5.4, and no catalogue accepts it."""
        vectors, stars = _ratings()
        model = taste.fit(vectors, stars)
        extreme = np.zeros(8)
        extreme[0] = 40.0
        for v in (extreme, -extreme, np.zeros(8)):
            guess = taste.predict(model, v / max(np.linalg.norm(v), 1e-9))
            self.assertIn(guess, (1, 2, 3, 4, 5))

    def test_a_crop_with_no_embedding_gets_no_prediction(self) -> None:
        vectors, stars = _ratings()
        self.assertIsNone(taste.predict(taste.fit(vectors, stars), None))

    def test_a_model_of_the_wrong_shape_is_refused(self) -> None:
        """An embedding model that changed size must not be read as weights
        for the new one."""
        self.assertIsNone(taste.predict({"weights": [1.0, 2.0]}, np.zeros(8)))


class DeferringToThePhotographer(unittest.TestCase):
    def test_a_hand_rating_outranks_a_prediction_in_the_sort(self) -> None:
        """The order the whole app believes things in: their answer, then
        what was learned from their other answers, then the measure."""
        from conrod import server

        self.assertTrue(server._RANK.startswith(
            "COALESCE(d.stars, d.predicted_stars,"))

    def test_the_measure_is_still_the_floor(self) -> None:
        """With nothing learned yet the cull still has an opinion."""
        from conrod import server

        self.assertIn("WHEN d.rating >=", server._RANK)


if __name__ == "__main__":
    unittest.main()
