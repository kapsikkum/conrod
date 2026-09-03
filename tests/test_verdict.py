"""Writing the cull's verdict onto a shoot that has already been culled once.

A shoot that has been through any first pass -- in the camera, in Photo
Mechanic, in Bridge -- carries a colour label on every single frame. Conrod
writes the label create-only, so on that shoot its verdict lands nowhere and
the cull looks like it did nothing at all. The rating does not have the same
problem, because a camera writes ``Rating=0`` for unrated and Conrod treats
zero as absent.

The two tags therefore need two switches, which is what these tests hold in
place. They run against real exiftool on a real sidecar, because the whole
question is what exiftool does with ``-wm cg`` and an ``-if`` condition, and a
fake would only assert what I already believe.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


class VerdictOnAnAlreadyCulledShoot(unittest.TestCase):
    def setUp(self):
        from conrod.config import find_exiftool

        try:
            self.exe = find_exiftool()
        except RuntimeError:
            self.skipTest("exiftool not installed")

    def _frame(self, tmp: Path, rating: str, label: str) -> Path:
        """A RAW whose sidecar already carries someone else's judgement."""
        from conrod import writer

        raw = tmp / "frame.CR3"
        raw.write_bytes(b"not really a raw, only the sidecar is written")
        sidecar = raw.with_suffix(".xmp")
        sidecar.write_text(writer._EMPTY_XMP, encoding="utf-8")
        subprocess.run(
            [self.exe, "-overwrite_original", f"-XMP:Rating={rating}",
             f"-XMP:Label={label}", str(sidecar)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return raw

    def _verdict(self, raw: Path, settings) -> tuple[str, str]:
        from conrod.exif import ExifTool
        from conrod import writer

        with ExifTool(self.exe) as tool:
            writer.write_keywords(tool, raw, [], settings, rating=2, label="Red")
        out = subprocess.run(
            [self.exe, "-s3", "-Rating", "-Label", str(raw.with_suffix(".xmp"))],
            capture_output=True, text=True, check=False)
        rating, label = out.stdout.strip().split("\n")
        return rating, label

    def _settings(self, **kw):
        from conrod.config import Settings

        settings = Settings()
        settings.write_sidecar_for_raw = True
        for key, value in kw.items():
            setattr(settings, key, value)
        return settings

    def test_a_label_already_present_blocks_the_cull_by_default(self) -> None:
        """The behaviour that made a cull appear to do nothing.

        This is not a bug being asserted as correct -- it is the deliberate
        conservative default, and the test exists so that turning it into a
        surprise again takes a deliberate edit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._frame(Path(tmp), "0", "Blue")
            rating, label = self._verdict(raw, self._settings())
        self.assertEqual(rating, "2", "unrated frames should still be rated")
        self.assertEqual(label, "Blue", "an existing colour is left alone")

    def test_overwrite_label_lets_the_cull_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._frame(Path(tmp), "0", "Blue")
            rating, label = self._verdict(
                raw, self._settings(overwrite_label=True))
        self.assertEqual(rating, "2")
        self.assertEqual(label, "Red")

    def test_the_two_switches_are_independent(self) -> None:
        """Replacing colours must not start replacing stars.

        They shared one flag before, so asking for one silently bought the
        other -- and the other one destroys a rating pass.
        """
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._frame(Path(tmp), "5", "Blue")
            rating, label = self._verdict(
                raw, self._settings(overwrite_label=True))
        self.assertEqual(rating, "5", "a five-star pick must survive")
        self.assertEqual(label, "Red")

    def test_overwriting_ratings_still_does_not_touch_the_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._frame(Path(tmp), "5", "Blue")
            rating, label = self._verdict(
                raw, self._settings(overwrite_rating=True))
        self.assertEqual(rating, "2")
        self.assertEqual(label, "Blue")

    def test_either_tag_can_be_switched_off_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._frame(Path(tmp), "0", "Blue")
            rating, label = self._verdict(
                raw, self._settings(write_rating=False, overwrite_label=True))
        self.assertEqual(rating, "0", "asked not to rate, so it did not")
        self.assertEqual(label, "Red")


class ReachableFromTheInterface(unittest.TestCase):
    """A setting nobody can find is the same as a setting that does not exist.

    All four of these existed in the config object and none appeared in the
    settings screen, so the only way to change one was to hand-edit JSON.
    """

    def test_every_verdict_setting_has_a_control(self) -> None:
        import conrod

        # conrod.web has no __init__.py, so its __file__ is None.
        source = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        for key in ("write_rating", "write_label",
                    "overwrite_rating", "overwrite_label"):
            self.assertIn(f'"{key}"', source, f"{key} is not in the settings UI")

    def test_the_settings_object_carries_them(self) -> None:
        from conrod.config import Settings

        data = Settings().to_dict()
        for key in ("write_rating", "write_label",
                    "overwrite_rating", "overwrite_label"):
            self.assertIn(key, data)
        self.assertFalse(data["overwrite_label"], "must default to safe")
        self.assertFalse(data["overwrite_rating"], "must default to safe")


if __name__ == "__main__":
    unittest.main()
