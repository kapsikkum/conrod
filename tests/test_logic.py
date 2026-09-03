"""Offline unit tests.

Everything here runs without models, without Ollama and without network, so CI
can prove the decision logic still holds on every push. The parts that need a
GPU or real photographs are covered by smoke_test.py, run by hand.

Several of these encode findings from real Bathurst frames; the comments say
which, because they look arbitrary otherwise.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conrod import plates
from conrod.analyze import VehicleAnalysis, _corroborated, _merge_number
from conrod.config import Settings
from conrod.culling import Cull
from conrod.keywords import for_frame, for_vehicle
from conrod.mapping import NumberMap
from conrod.ocr import Reading, _normalise, _plausible


class PlateFormats(unittest.TestCase):
    def test_real_plates_are_recognised(self):
        # Both read off actual frames during development.
        self.assertTrue(plates.looks_like_plate("FD23RS"))   # NSW, Focus RS
        self.assertTrue(plates.looks_like_plate("LA93NG"))   # NSW, Ford Ranger

    def test_race_numbers_are_not_plates(self):
        for token in ("21", "71", "8", "220"):
            self.assertFalse(plates.looks_like_plate(token), token)

    def test_trim_recovers_a_plate_from_a_stray_character(self):
        # OCR read the Ranger's plate as "ELA93NG": it picked up the plate
        # frame as a leading character. The trimmed form matches a real issue
        # format and the untrimmed one matches nothing.
        self.assertEqual(plates._trim_to_format("ELA93NG"), "LA93NG")
        self.assertEqual(plates._trim_to_format("ABC123X"), "ABC123")

    def test_trim_leaves_valid_plates_alone(self):
        for token in ("FD23RS", "LA93NG", "ABC123"):
            self.assertEqual(plates._trim_to_format(token), token)

    def test_trim_refuses_to_mangle_short_tokens(self):
        self.assertEqual(plates._trim_to_format("XY"), "XY")

    def test_state_badging_is_not_read_as_the_registration(self):
        settings = Settings()
        reading = plates._interpret(
            [("NSW", 0.71), ("FD23RS", 0.84), ("THEFIRSTSTATE", 0.5)], settings)
        self.assertEqual(reading.text, "FD23RS")
        self.assertEqual(reading.state, "NSW")


class NumberReading(unittest.TestCase):
    def test_plausible_lengths(self):
        settings = Settings()
        for token, expected in [("7", True), ("21", True), ("123", True),
                                ("1234", False), ("0007", False),
                                ("", False), ("ABC", False)]:
            self.assertIs(_plausible(token, settings), expected, token)

    def test_lookalikes_bend_only_near_numeric_tokens(self):
        # "BOSS" is a sponsor; bending it to 8055 would be a silent error.
        self.assertEqual(_normalise("BOSS"), "BOSS")
        self.assertEqual(_normalise("4O"), "40")

    def test_agreement_between_readers_raises_confidence(self):
        settings = Settings()
        number, source, conf = _merge_number(Reading("21", 0.55, "ocr"), "21", settings)
        self.assertEqual(number, "21")
        self.assertIn("vlm", source)
        self.assertGreater(conf, 0.55)

    def test_roundel_source_is_carried_through(self):
        settings = Settings()
        _, source, _ = _merge_number(Reading("21", 0.9, "roundel"), "21", settings)
        self.assertEqual(source, "roundel+vlm")

    def test_weak_ocr_still_surfaces_for_review(self):
        settings = Settings()
        number, _, conf = _merge_number(Reading("8", 0.2, "ocr"), None, settings)
        self.assertEqual(number, "8")
        self.assertLess(conf, settings.ocr_accept_confidence)


class TeamCorroboration(unittest.TestCase):
    def test_team_backed_by_read_text_is_corroborated(self):
        # "RECKLESS BREWING" was reported by the model and OCR read both words.
        self.assertTrue(_corroborated("RECKLESS BREWING", ["RECKLESS", "BREWING"]))

    def test_invented_team_is_not_corroborated(self):
        # The Mini produced "Nosso" from the garbled tokens below.
        self.assertFalse(_corroborated("Nosso", ["PRO", "Nos8e", "No886"]))

    def test_generic_words_alone_do_not_corroborate(self):
        self.assertFalse(_corroborated("Racing Team", ["RACING", "TEAM"]))


class Mapping(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.csv = Path(self.dir.name) / "entries.csv"
        self.csv.write_text(
            "number,driver,team\n"
            "88,Broc Feeney,Triple Eight\n"
            "07,Test Driver,Privateer;Backup Team\n",
            encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def test_leading_zeros_match(self):
        mapping = NumberMap.load(self.csv)
        self.assertIn("Test Driver", mapping.keywords_for("7"))

    def test_multi_value_cells_split(self):
        mapping = NumberMap.load(self.csv)
        self.assertIn("Backup Team", mapping.keywords_for("07"))

    def test_missing_number_column_is_an_error(self):
        bad = Path(self.dir.name) / "bad.csv"
        bad.write_text("driver,team\nA,B\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            NumberMap.load(bad)


class Keywords(unittest.TestCase):
    def test_vehicle_keywords_cover_the_useful_searches(self):
        analysis = VehicleAnalysis(
            race_number="21", make="MINI", model="Cooper S", colour="black",
            body_type="hatchback", plate="FD23RS", plate_state="NSW")
        words = for_vehicle(analysis, Settings())
        for expected in ("21", "#21", "Car 21", "MINI", "Cooper S",
                         "MINI Cooper S", "black", "FD23RS", "NSW"):
            self.assertIn(expected, words)

    def test_uncorroborated_team_is_withheld(self):
        analysis = VehicleAnalysis(team="Nosso", team_corroborated=False)
        self.assertNotIn("Nosso", for_vehicle(analysis, Settings()))

    def test_corroborated_team_is_written(self):
        analysis = VehicleAnalysis(team="Reckless Brewing", team_corroborated=True)
        self.assertIn("Reckless Brewing", for_vehicle(analysis, Settings()))

    def test_plate_can_be_suppressed(self):
        settings = Settings()
        settings.write_plate_keyword = False
        analysis = VehicleAnalysis(plate="FD23RS")
        self.assertNotIn("FD23RS", for_vehicle(analysis, settings))

    def test_prefix_is_applied_to_everything(self):
        settings = Settings()
        settings.keyword_prefix = "TA:"
        words = for_vehicle(VehicleAnalysis(race_number="21"), settings)
        self.assertTrue(all(w.startswith("TA:") for w in words), words)

    def test_frame_keywords_are_the_union_without_duplicates(self):
        one = VehicleAnalysis(race_number="21", make="MINI")
        two = VehicleAnalysis(race_number="71", make="MINI")
        words = for_frame([one, two], Settings())
        self.assertEqual(len(words), len(set(words)))
        self.assertIn("21", words)
        self.assertIn("71", words)


class Culling(unittest.TestCase):
    def test_rejected_frames_are_skipped(self):
        ok, why = Cull(rating=0, rejected=True).passes(Settings())
        self.assertFalse(ok)
        self.assertEqual(why, "rejected")

    def test_star_threshold(self):
        settings = Settings()
        settings.min_rating = 3
        self.assertFalse(Cull(rating=2).passes(settings)[0])
        self.assertTrue(Cull(rating=3).passes(settings)[0])

    def test_unrated_frames_pass_when_no_threshold_is_set(self):
        self.assertTrue(Cull().passes(Settings())[0])

    def test_label_filter(self):
        settings = Settings()
        settings.require_label = "Green"
        self.assertTrue(Cull(label="Green").passes(settings)[0])
        self.assertFalse(Cull(label="Red").passes(settings)[0])


class SettingsRoundTrip(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = Settings()
            settings.detect_conf = 0.42
            settings.include_bikes = False
            settings.save(path)
            loaded = Settings.load(path)
            self.assertEqual(loaded.detect_conf, 0.42)
            self.assertFalse(loaded.include_bikes)

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"detect_conf": 0.3, "from_a_future_build": 1}',
                            encoding="utf-8")
            self.assertEqual(Settings.load(path).detect_conf, 0.3)

    def test_active_classes_follow_the_switches(self):
        settings = Settings()
        settings.include_cars = True
        settings.include_bikes = False
        settings.include_trucks = False
        self.assertEqual(settings.active_classes(), [2])
        settings.include_bikes = True
        self.assertIn(3, settings.active_classes())


class ExistingMetadataIsPreserved(unittest.TestCase):
    """A shoot is usually keyworded after it has been culled and captioned,
    so the write must add to the sidecar rather than replace it. Keywords
    merge on their own; a description is a single value and an earlier
    version replaced it, losing captions typed in Lightroom."""

    def setUp(self):
        from conrod.config import find_exiftool

        try:
            find_exiftool()
        except RuntimeError:
            self.skipTest("exiftool not installed")

    def test_rating_label_keywords_and_caption_all_survive(self):
        import subprocess
        from conrod.config import Settings, find_exiftool
        from conrod.exif import ExifTool
        from conrod import writer

        exe = find_exiftool()
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "frame.CR3"
            raw.write_bytes(b"not really a raw, only the sidecar is written")
            sidecar = raw.with_suffix(".xmp")
            sidecar.write_text(writer._EMPTY_XMP, encoding="utf-8")
            subprocess.run([
                exe, "-overwrite_original", "-XMP:Rating=4", "-XMP:Label=Yellow",
                "-XMP-dc:Subject=Bathurst", "-XMP-dc:Description=Mine, not yours",
                str(sidecar)], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False)

            settings = Settings()
            settings.write_sidecar_for_raw = True
            with ExifTool(exe) as tool:
                writer.write_keywords(tool, raw, ["#21", "Mini"], settings,
                                      caption="generated")

            out = subprocess.run(
                [exe, "-s3", "-Rating", "-Label", "-Subject", "-Description",
                 str(sidecar)], capture_output=True, text=True, check=False)
            rating, label, subject, description = out.stdout.strip().split("\n")

        self.assertEqual(rating, "4")
        self.assertEqual(label, "Yellow")
        self.assertIn("Bathurst", subject)      # theirs kept
        self.assertIn("#21", subject)           # ours added
        self.assertEqual(description, "Mine, not yours")   # never replaced


if __name__ == "__main__":
    unittest.main()
