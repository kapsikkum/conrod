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


def _photo_texture(width: int, height: int, seed: int = 7) -> Image.Image:
    """Detail as a camera records it, rather than as a random number
    generator produces it.

    ``_texture`` is per-pixel noise, which carries energy right up to the
    sampling limit. Nothing photographed through a lens does: the glass and
    the sensor band-limit it long before that, and what is left sits on
    edges -- panel gaps, badges, the line of a spoiler -- rather than in
    every pixel. The difference decides the top of the scale: against white
    noise the measure still reads its maximum after two pixels of blur, so
    anything calibrated on it puts the sharp end where no photograph can
    reach and drops the whole shoot into the bottom half.

    Pre-blurring the noise instead does not work either, and the reason is
    worth writing down: blurs add in quadrature, so once the fixture is soft
    enough to be in range, another pixel on top of it is an eight percent
    change rather than the step off crisp that it is on a real frame.
    """
    rng = np.random.default_rng(seed)
    data = np.full((height, width), 40, dtype=np.uint8)
    for _ in range(60):                                   # panels and glass
        x, y = rng.integers(0, width - 60), rng.integers(0, height - 50)
        data[y:y + rng.integers(6, 50), x:x + rng.integers(8, 60)] = (
            rng.integers(90, 235))
    for _ in range(120):                                  # gaps and badges
        x, y = rng.integers(0, width - 30), rng.integers(0, height - 30)
        if rng.integers(0, 2):
            data[y:y + rng.integers(1, 3), x:x + rng.integers(6, 30)] = (
                rng.integers(90, 235))
        else:
            data[y:y + rng.integers(6, 30), x:x + rng.integers(1, 3)] = (
                rng.integers(90, 235))
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

        On a photographic fixture rather than white noise, for the reason
        given on ``_photo_texture``: the scale is calibrated for pictures,
        and noise sits off the end of it.
        """
        base = _photo_texture(600, 400)
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


class PanningShots(unittest.TestCase):
    """The frame the photographer went there to take.

    A held pan is mostly blur on purpose: the background is smeared and only
    the car is meant to be crisp. Judged on the whole picture it is the worst
    frame of the set, and an automatic cull throws away the keepers.
    """

    @staticmethod
    def _frame(subject_sharp: bool, background_sharp: bool, size=(600, 400)):
        """A crop with a vehicle-sized box in it, each part blurred or not."""
        from PIL import ImageFilter
        rng = np.random.default_rng(7)
        noise = rng.integers(0, 255, (size[1], size[0]), dtype=np.uint8)
        img = Image.fromarray(noise, "L").convert("RGB")
        box = (150, 100, 450, 300)

        def maybe_blur(part, sharp):
            return part if sharp else part.filter(ImageFilter.GaussianBlur(6))

        base = maybe_blur(img, background_sharp)
        subject = maybe_blur(img.crop(box), subject_sharp)
        base.paste(subject, box[:2])
        return base, box

    def test_a_pan_is_recognised_and_never_culled(self) -> None:
        image, box = self._frame(subject_sharp=True, background_sharp=False)
        out = sharpness.measure(image, box)
        self.assertTrue(out.panning, f"subject {out.score} background {out.background}")
        self.assertFalse(sharpness.cullable(out, "poor"),
                         "a held pan was offered up for automatic culling")

    def test_a_genuinely_missed_frame_is_not_a_pan(self) -> None:
        """Everything moved, not just the background."""
        image, box = self._frame(subject_sharp=False, background_sharp=False)
        out = sharpness.measure(image, box)
        self.assertFalse(out.panning)
        self.assertTrue(sharpness.cullable(out, "poor"))

    def test_a_sharp_car_against_a_sharp_background_is_not_a_pan(self) -> None:
        image, box = self._frame(subject_sharp=True, background_sharp=True)
        self.assertFalse(sharpness.measure(image, box).panning)

    def test_the_background_does_not_decide_the_subject_score(self) -> None:
        """The complaint this was built for.

        The same car, once against a smeared background and once against a
        sharp one. The subject is identical, so its score must be too.
        """
        blurred_bg, box = self._frame(subject_sharp=True, background_sharp=False)
        sharp_bg, _ = self._frame(subject_sharp=True, background_sharp=True)
        a = sharpness.measure(blurred_bg, box).score
        b = sharpness.measure(sharp_bg, box).score
        self.assertAlmostEqual(a, b, delta=0.05,
                               msg="the background moved the subject's score")

    def test_a_soft_car_is_not_rescued_by_a_sharp_background(self) -> None:
        """The larger half of the same defect, and the more dangerous one.

        A high percentile already leans towards the sharpest tiles, so a
        smeared background costs a good pan comparatively little. What it
        cannot survive is the reverse: a soft car in front of a sharp fence
        or a banner, where the background *is* the sharpest thing in the crop
        and the frame gets called sharp on the strength of it.

        Measured across 1,256 real detections, the whole-crop score ran as
        much as 0.30 above the subject's own, and the rating changed on about
        one detection in five.
        """
        image, box = self._frame(subject_sharp=False, background_sharp=True)
        whole = sharpness.measure(image).score
        subject = sharpness.measure(image, box).score
        # Relative, not absolute: the thresholds were calibrated on real
        # photographs, and a synthetic noise target sits nowhere near where a
        # car does on the scale. What is being asserted is the direction of
        # the error, which is the part that was wrong.
        self.assertGreater(whole, subject + 0.15,
                           "the sharp background did not contaminate the old measure")


class DoubtfulCulls(unittest.TestCase):
    """Cull, but say so when it was a close call."""

    def test_a_comfortable_cull_is_not_flagged(self) -> None:
        result = sharpness.Sharpness(score=0.02, verdict="blurred")
        self.assertFalse(sharpness.doubtful(result, "poor", 0.25))

    def test_a_score_on_the_line_is_flagged(self) -> None:
        result = sharpness.Sharpness(score=0.26, verdict="blurred")
        self.assertTrue(sharpness.doubtful(result, "poor", 0.25))

    def test_one_sharp_end_is_flagged(self) -> None:
        """A pan held on the nose averages out to blurred over the whole car."""
        result = sharpness.Sharpness(score=0.10, verdict="blurred",
                                     bands=(0.55, 0.20, 0.05), sharp_end="left")
        self.assertTrue(result.partly_sharp)
        self.assertTrue(sharpness.doubtful(result, "poor", 0.25))

    def test_frames_that_are_kept_are_never_flagged(self) -> None:
        """Uncertainty is only interesting about a frame being thrown away.

        Flagging everything near any threshold queued two thirds of a real
        shoot for review, which is the same as flagging nothing.
        """
        result = sharpness.Sharpness(score=0.53, verdict="soft",
                                     bands=(0.7, 0.3, 0.2), sharp_end="left")
        self.assertFalse(sharpness.doubtful(result, "good", 0.25))
        self.assertFalse(sharpness.doubtful(result, "fair", 0.25))


class StarsAndLabels(unittest.TestCase):
    """The verdict as a catalogue understands it."""

    def test_the_bands_run_the_right_way(self) -> None:
        self.assertGreater(sharpness.stars_for(0.9), sharpness.stars_for(0.4))
        self.assertEqual(sharpness.stars_for(0.0), 1)

    def test_the_whole_scale_is_reachable(self) -> None:
        """The measure used to stop at four, on the grounds that whether the
        moment is any good is not a focus measurement. It is the
        photographer's scale though, and they can overrule any of it -- so
        the cull gives its opinion across the range instead.

        The bands were also calibrated above what the measure actually
        produces: the top one began at 0.72 when the highest rating over
        1,720 real frames was 0.711, so four was unreachable and the whole
        shoot squashed into one to three."""
        reachable = {sharpness.stars_for(r / 100) for r in range(101)}
        self.assertEqual(reachable, {1, 2, 3, 4, 5})

    def test_colours_match_the_verdicts(self) -> None:
        self.assertEqual(sharpness.label_for("good"), "Green")
        self.assertEqual(sharpness.label_for("poor"), "Red")
        self.assertEqual(sharpness.label_for("fair"), "Yellow")


class WhereTheSharpnessIs(unittest.TestCase):
    """Which end of the car is sharp -- in image terms, not front and back."""

    @staticmethod
    def _split(left_sharp: bool):
        from PIL import ImageFilter
        rng = np.random.default_rng(11)
        noise = rng.integers(0, 255, (300, 600), dtype=np.uint8)
        img = Image.fromarray(noise, "L").convert("RGB")
        half = img.crop((0, 0, 300, 300) if left_sharp else (300, 0, 600, 300))
        blurred = img.filter(ImageFilter.GaussianBlur(7))
        blurred.paste(half, (0, 0) if left_sharp else (300, 0))
        return blurred

    def test_a_sharp_left_end_is_reported(self) -> None:
        out = sharpness.measure(self._split(True), (0, 0, 600, 300))
        self.assertEqual(out.sharp_end, "left")

    def test_a_sharp_right_end_is_reported(self) -> None:
        out = sharpness.measure(self._split(False), (0, 0, 600, 300))
        self.assertEqual(out.sharp_end, "right")

    def test_an_evenly_sharp_car_says_even(self) -> None:
        rng = np.random.default_rng(3)
        noise = rng.integers(0, 255, (300, 600), dtype=np.uint8)
        img = Image.fromarray(noise, "L").convert("RGB")
        self.assertEqual(sharpness.measure(img, (0, 0, 600, 300)).sharp_end, "even")


if __name__ == "__main__":
    unittest.main()
