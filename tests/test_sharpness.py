"""Subject sharpness, and the motorsport cases that break the naive version.

Synthetic images, deliberately. A real panning shot would prove the measure
works on that one photograph; a synthesised one proves it works on the
*property* -- sharp subject, smeared surroundings -- which is what the code
claims to measure.
"""

from __future__ import annotations

import unittest

import numpy as np
from PIL import Image, ImageFilter

from conrod import sharpness


def _texture(width: int, height: int, seed: int = 7) -> Image.Image:
    """Detailed noise: something with real edges at every scale."""
    rng = np.random.default_rng(seed)
    data = rng.integers(40, 215, size=(height, width), dtype=np.uint8)
    return Image.fromarray(data, mode="L").convert("RGB")


def _flat(width: int, height: int, value: int = 128) -> Image.Image:
    return Image.new("RGB", (width, height), (value, value, value))


def _paste(background: Image.Image, patch: Image.Image, box) -> Image.Image:
    out = background.copy()
    out.paste(patch, box)
    return out


class Basics(unittest.TestCase):
    def test_a_sharp_crop_scores_above_a_blurred_one(self) -> None:
        sharp = sharpness.measure(_texture(400, 300))
        blurred = sharpness.measure(
            _texture(400, 300).filter(ImageFilter.GaussianBlur(4)))
        self.assertGreater(sharp.score, blurred.score)

    def test_blur_is_ordered_not_just_detected(self) -> None:
        """More blur must score lower than less, or sorting a shoot is noise."""
        scores = [sharpness.measure(
            _texture(400, 300).filter(ImageFilter.GaussianBlur(r))).score
            for r in (0.5, 1.5, 3.0, 6.0)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_a_crop_too_small_to_judge_says_so(self) -> None:
        self.assertFalse(sharpness.measure(_texture(8, 8)))

    def test_a_featureless_crop_says_so(self) -> None:
        """No tile has the contrast to carry an opinion."""
        self.assertFalse(sharpness.measure(_flat(400, 300)))


class Motorsport(unittest.TestCase):
    """The cases a whole-frame focus measure gets backwards."""

    def test_a_panned_subject_beats_a_wholly_blurred_frame(self) -> None:
        """Sharp car, smeared everything else. The point of the exercise.

        A mean-based measure scores this below a frame that is uniformly
        soft, because most of the pixels are blur. It is the better picture
        and has to score as one.
        """
        panned = _paste(
            _texture(600, 400).filter(ImageFilter.GaussianBlur(7)),
            _texture(260, 170, seed=3), (170, 115))
        all_soft = _texture(600, 400).filter(ImageFilter.GaussianBlur(3))
        self.assertGreater(sharpness.measure(panned).score,
                           sharpness.measure(all_soft).score)

    def test_spinning_wheels_do_not_condemn_a_sharp_body(self) -> None:
        """A minority of blurred tiles must not decide for the whole crop."""
        body = _texture(600, 400, seed=11)
        wheels = _paste(body, body.crop((40, 300, 190, 400))
                        .filter(ImageFilter.GaussianBlur(6)), (40, 300))
        wheels = _paste(wheels, body.crop((410, 300, 560, 400))
                        .filter(ImageFilter.GaussianBlur(6)), (410, 300))
        self.assertGreater(
            sharpness.measure(wheels).score,
            sharpness.measure(body.filter(ImageFilter.GaussianBlur(6))).score)

    def test_a_dark_car_is_not_marked_down_for_being_dark(self) -> None:
        """Normalising by contrast is what stops exposure reading as focus."""
        bright = _texture(400, 300)
        dark = Image.fromarray(
            (np.asarray(bright, dtype=np.float32) * 0.28).astype(np.uint8))
        self.assertAlmostEqual(sharpness.measure(bright).score,
                               sharpness.measure(dark).score, delta=0.08)

    def test_the_same_crop_scores_the_same_at_any_size(self) -> None:
        """Otherwise a big crop looks sharper for being big."""
        big = _texture(1200, 900)
        small = big.resize((400, 300), Image.LANCZOS)
        self.assertAlmostEqual(sharpness.measure(big).score,
                               sharpness.measure(small).score, delta=0.12)


class Verdicts(unittest.TestCase):
    def test_thresholds_name_the_bands(self) -> None:
        self.assertEqual(sharpness.verdict_for(0.80, 0.62, 0.40), "sharp")
        self.assertEqual(sharpness.verdict_for(0.50, 0.62, 0.40), "soft")
        self.assertEqual(sharpness.verdict_for(0.20, 0.62, 0.40), "blurred")

    def test_a_badly_blurred_crop_is_blurred_not_unknown(self) -> None:
        """The bug this class exists for.

        Blurring something flattens it, so a contrast floor set high enough
        to exclude sky also excluded every tile of a hopeless frame -- and
        the worst pictures in the shoot came back "cannot tell", which is the
        one answer that helps nobody.
        """
        gone = _texture(600, 400).filter(ImageFilter.GaussianBlur(8))
        self.assertEqual(sharpness.rate(gone).verdict, "blurred")

    def test_the_scale_spreads_the_judgements_that_matter(self) -> None:
        """Crisp, usable, soft and lost have to be far apart to sort by.

        A saturating curve put everything from pin-sharp to visibly soft
        inside four points of each other.
        """
        base = _texture(600, 400)
        crisp = sharpness.measure(base).score
        usable = sharpness.measure(
            base.filter(ImageFilter.GaussianBlur(1))).score
        soft = sharpness.measure(
            base.filter(ImageFilter.GaussianBlur(3))).score
        self.assertGreater(crisp - usable, 0.05)
        self.assertGreater(usable - soft, 0.3)

    def test_an_unmeasurable_crop_gets_no_verdict(self) -> None:
        """Better to say nothing than to call a flat crop blurred."""
        self.assertEqual(sharpness.rate(_flat(400, 300)).verdict, "unknown")


if __name__ == "__main__":
    unittest.main()
