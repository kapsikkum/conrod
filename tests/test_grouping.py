"""Grouping and consensus.

These encode what eight frames of one blue Ford Falcon actually produced when
qwen2.5vl:7b was asked to name it: three Fairmonts, two Mustangs, a Fiesta, a
"Vauxhall Astra" and a Commodore. Grouping exists because that spread is
normal, so the tests use the real spread rather than a tidy invented one.
"""

from __future__ import annotations

import unittest

from conrod.grouping import consensus

# What the model returned for the eight Falcon frames, verbatim.
FALCON = (
    [{"make": "Ford", "model": "Fairmont", "colour": "blue"}] * 3
    + [{"make": "Ford", "model": "Mustang", "colour": "blue"}] * 2
    + [
        {"make": "Ford", "model": "Fiesta", "colour": "blue"},
        {"make": "Holden", "model": "Vauxhall Astra", "colour": "blue"},
        {"make": "Holden", "model": "Holden Commodore", "colour": "blue"},
    ]
)


class Consensus(unittest.TestCase):
    def test_a_disputed_group_keeps_the_make_the_majority_agreed_on(self):
        # No model name has a majority, but six of eight said Ford.
        out = consensus(FALCON)
        self.assertEqual(out.make, "Ford")
        self.assertIsNone(out.model)
        self.assertAlmostEqual(out.agreement, 0.75)

    def test_the_disagreement_is_still_reported(self):
        out = consensus(FALCON)
        self.assertIn("Ford Fairmont", out.disputed)
        self.assertIn("Holden Vauxhall Astra", out.disputed)

    def test_colour_survives_when_the_name_does_not(self):
        self.assertEqual(consensus(FALCON).colour, "blue")

    def test_no_make_majority_means_no_name_at_all(self):
        # Two Holdens out of four is not enough to write "Holden" down.
        out = consensus([
            {"make": "Ford", "model": "Fiesta"},
            {"make": "Holden", "model": "Astra"},
            {"make": "Holden", "model": "Commodore"},
            {"make": "Toyota", "model": "Corolla"},
        ])
        self.assertIsNone(out.make)
        self.assertIsNone(out.model)

    def test_make_and_model_are_never_voted_apart(self):
        # The original bug: voting the fields separately produced "Holden
        # Fiesta" -- a name no frame gave and no car has.
        out = consensus([
            {"make": "Ford", "model": "Fiesta"},
            {"make": "Holden", "model": "Astra"},
            {"make": "Holden", "model": "Commodore"},
        ])
        self.assertNotEqual((out.make, out.model), ("Holden", "Fiesta"))

    def test_an_agreeing_group_keeps_the_full_name(self):
        out = consensus([{"make": "Mitsubishi", "model": "Outlander",
                          "colour": "grey"}] * 3)
        self.assertEqual((out.make, out.model), ("Mitsubishi", "Outlander"))
        self.assertEqual(out.agreement, 1.0)
        self.assertEqual(out.disputed, [])

    def test_plates_are_taken_from_the_most_confident_read_not_voted(self):
        out = consensus([
            {"make": "Ford", "model": "Falcon", "plate": "AAA11A", "plate_conf": 0.4},
            {"make": "Ford", "model": "Falcon", "plate": "AAA11A", "plate_conf": 0.4},
            {"make": "Ford", "model": "Falcon", "plate": "73111J", "plate_conf": 0.91},
        ])
        self.assertEqual(out.plate, "73111J")


if __name__ == "__main__":
    unittest.main()


class Nameplates(unittest.TestCase):
    """A make that contradicts the model name beside it."""

    def test_the_nameplate_corrects_a_wrong_badge(self):
        from conrod.marques import correct_make

        # Measured: qwen2.5vl:7b called a Kawasaki Ninja H2 a Yamaha, while
        # naming the model exactly right.
        self.assertEqual(correct_make("Yamaha", "Ninja H2"), "Kawasaki")
        self.assertEqual(correct_make("Holden", "Fiesta"), "Ford")

    def test_an_agreeing_pair_is_left_alone(self):
        from conrod.marques import correct_make

        self.assertEqual(correct_make("Ford", "Falcon XR8"), "Ford")
        self.assertEqual(correct_make("Mazda", "RX-8"), "Mazda")

    def test_a_nameplate_everyone_sells_corrects_nothing(self):
        from conrod.marques import correct_make

        # "GT", "RS" and "Sport" are not anybody's exclusively.
        self.assertEqual(correct_make("Ford", "GT"), "Ford")
        self.assertEqual(correct_make("Holden", "RS Sport"), "Holden")

    def test_a_missing_make_is_filled_in_from_the_nameplate(self):
        from conrod.marques import correct_make

        self.assertEqual(correct_make(None, "Commodore"), "Holden")


class PlateIsAnIdentity(unittest.TestCase):
    """A read plate outranks every other grouping signal."""

    SIG_A = "ffff0000:" + ",".join(["0.03"] * 36)
    SIG_B = "0000ffff:" + ",".join(["0.03"] * 36)

    def test_the_same_plate_groups_crops_that_look_nothing_alike(self):
        from conrod.grouping import cluster

        # The real case: one purple Falcon side-on in sun and from behind in
        # shade, forty frames apart, sampled as two different colours.
        rows = [(1, self.SIG_A, 1, "#4b2d6e", "car", "Ford", "EYU-06S"),
                (2, self.SIG_B, 41, "#2e1a44", "car", "Ford", "EYU06S")]
        groups = set(cluster(rows).values())
        self.assertEqual(len(groups), 1)

    def test_different_plates_stay_apart_however_alike_they_look(self):
        from conrod.grouping import cluster

        rows = [(1, self.SIG_A, 1, "#1a729c", "car", "Ford", "ABC12D"),
                (2, self.SIG_A, 2, "#1a729c", "car", "Ford", "XYZ99Z")]
        self.assertEqual(len(set(cluster(rows).values())), 2)


class MisreadPlates(unittest.TestCase):
    """One character apart is one plate, not two vehicles.

    The measured case: a blue Ford read as 43111J across eleven frames and
    73111J across three more. Because a plate is identity, the mismatch was
    proof of *different* vehicles, so one car became several -- and the
    frames that read the car correctly never got to outvote the frames the
    vision model had called a Holden.
    """

    SIG = "ffff0000:" + ",".join(["0.03"] * 36)
    OTHER = "0000ffff:" + ",".join(["0.03"] * 36)

    def test_a_confusable_character_does_not_split_a_vehicle(self):
        from conrod.grouping import cluster

        rows = [(1, self.SIG, 1, "#2b3f67", "car", "Ford", "43111J"),
                (2, self.SIG, 2, "#2c426c", "car", "Ford", "73111J")]
        self.assertEqual(len(set(cluster(rows).values())), 1)

    def test_a_near_plate_outranks_a_disagreeing_make(self):
        """The circularity this was written for.

        The model called the same car a Ford in one frame and a Holden in the
        next. The make gate then refused the merge, which preserved the wrong
        answer. A measured plate beats the model's opinion.
        """
        from conrod.grouping import cluster

        rows = [(1, self.SIG, 1, "#2b3f67", "car", "Ford", "43111J"),
                (2, self.SIG, 2, "#2c426c", "car", "Holden", "73111J")]
        self.assertEqual(len(set(cluster(rows).values())), 1)

    def test_it_does_not_override_the_paint(self):
        """A near plate is evidence, not a licence to merge anything.

        Colour is measured off the crop, so unlike the make it is not the
        model's opinion and does not get overruled.
        """
        from conrod.grouping import cluster

        rows = [(1, self.SIG, 1, "#2b3f67", "car", "Ford", "43111J"),
                (2, self.SIG, 2, "#c0392b", "car", "Ford", "73111J")]
        self.assertEqual(len(set(cluster(rows).values())), 2)

    def test_two_genuinely_different_plates_still_split(self):
        from conrod.grouping import cluster

        rows = [(1, self.SIG, 1, "#2b3f67", "car", "Ford", "43111J"),
                (2, self.SIG, 2, "#2b3f67", "car", "Ford", "98222K")]
        self.assertEqual(len(set(cluster(rows).values())), 2)

    def test_only_confusable_characters_count(self):
        """43111J against 45111J differs by a character no reader confuses."""
        from conrod.grouping import _near_plate

        self.assertTrue(_near_plate("43111J", "73111J"))    # 4 <-> 7
        self.assertTrue(_near_plate("8BC123", "BBC123"))    # 8 <-> B
        self.assertFalse(_near_plate("43111J", "45111J"))   # 3 <-> 5
        self.assertFalse(_near_plate("43111J", "43111"))    # different lengths
        self.assertFalse(_near_plate("43111J", "73112J"))   # two apart


class BurstsGroup(unittest.TestCase):
    """A burst is one run of the shutter at one subject.

    That is a much better statement than "their file ids are close together",
    which is all the frame window ever knew. It counted a second shooter's
    frames as adjacent because the two cameras interleave in one folder, and
    counted a burst as distant when the cull had taken six frames out of the
    middle of it.
    """

    SIG = "ffff0000:" + ",".join(["0.03"] * 36)
    OTHER = "0000ffff:" + ",".join(["0.03"] * 36)

    def test_a_burst_holds_together_across_a_gap_in_file_ids(self):
        from conrod.grouping import cluster

        # Frames 1 and 40: far apart in the folder, one burst in time.
        rows = [(1, self.SIG, 1, "#2b3f67", "car", None, None, 7),
                (2, self.OTHER, 40, "#2c426c", "car", None, None, 7)]
        self.assertEqual(len(set(cluster(rows).values())), 1)

    def test_different_bursts_are_not_forced_together(self):
        from conrod.grouping import cluster

        rows = [(1, self.SIG, 1, "#2b3f67", "car", None, None, 7),
                (2, self.OTHER, 40, "#2c426c", "car", None, None, 9)]
        self.assertEqual(len(set(cluster(rows).values())), 2)

    def test_a_burst_does_not_override_the_paint(self):
        """Two cars can be in one burst. Colour still has to agree."""
        from conrod.grouping import cluster

        rows = [(1, self.SIG, 1, "#2b3f67", "car", None, None, 7),
                (2, self.SIG, 2, "#c0392b", "car", None, None, 7)]
        self.assertEqual(len(set(cluster(rows).values())), 2)

    def test_frames_with_no_burst_behave_as_before(self):
        """A folder with no EXIF still groups on the old signals."""
        from conrod.grouping import cluster

        rows = [(1, self.SIG, 1, "#2b3f67", "car", None, None, None),
                (2, self.SIG, 2, "#2c426c", "car", None, None, None)]
        self.assertEqual(len(set(cluster(rows).values())), 1)


class SecondLook(unittest.TestCase):
    """Which frames get shown to the model when the readings disagree."""

    def test_the_sharpest_frames_are_the_ones_sent(self):
        """The disagreement is usually caused by the blurred frames.

        Sending them again is asking the same question of the same bad
        evidence. Sharpness ranking is what makes the second look worth
        making at all.
        """
        from unittest.mock import patch
        from conrod import grouping

        crops = {1: ("blurred.jpg", 0.11), 2: ("sharp.jpg", 0.94),
                 3: ("soft.jpg", 0.48), 4: ("sharpest.jpg", 0.98)}
        sent = {}

        def spy(paths, settings, **kw):
            sent["paths"] = [p.name for p in paths]
            class Seen:
                make, model = "Ford", "Falcon FG"
            return Seen()

        with patch("conrod.vlm.identify_burst", spy):
            grouping._second_look([1, 2, 3, 4], crops, None)
        self.assertEqual(sent["paths"], ["sharpest.jpg", "sharp.jpg", "soft.jpg"])
        self.assertNotIn("blurred.jpg", sent["paths"])

    def test_no_crops_means_no_call(self):
        from unittest.mock import patch
        from conrod import grouping

        def explode(*a, **k):
            raise AssertionError("asked the model about nothing")

        with patch("conrod.vlm.identify_burst", explode):
            out = grouping._second_look([1, 2], {}, None)
        self.assertFalse(out.make)


class Accumulation(unittest.TestCase):
    """What one frame saw and another could not."""

    # The two frames of the purple Falcon: one read the team off the door,
    # the other read the number off the boot.
    PAIR = [
        {"make": "Ford", "model": "Falcon FG", "colour": "Purple",
         "plate": "EYU06S", "plate_conf": 0.94, "team": "CV Performance",
         "sponsors": ["CV Performance"], "colour_hex": "#4b2d6e"},
        {"make": "Ford", "model": "FG Falcon XR8", "colour": "blue",
         "plate": "EYU06S", "plate_conf": 0.6, "race_number": "06",
         "number_conf": 0.70, "sponsors": ["FPV"], "colour_hex": "#2e1a44"},
    ]

    def test_the_number_seen_in_one_frame_reaches_the_group(self):
        self.assertEqual(consensus(self.PAIR).race_number, "06")

    def test_the_team_seen_in_one_frame_reaches_the_group(self):
        self.assertEqual(consensus(self.PAIR).team, "CV Performance")

    def test_sponsors_are_pooled_rather_than_voted(self):
        # A majority vote would keep one and discard the other, when both are
        # really on the car and each frame only saw the side facing it.
        self.assertEqual(set(consensus(self.PAIR).sponsors),
                         {"CV Performance", "FPV"})

    def test_the_most_confident_plate_read_wins(self):
        self.assertEqual(consensus(self.PAIR).plate, "EYU06S")
