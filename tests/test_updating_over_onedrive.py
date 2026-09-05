"""Unpacking a new build when last time's folder will not go away.

Reported with a screenshot: 0.6.3 downloaded, 305 MB on disk, nothing wrong
with it, and the update refusing with

    [WinError 183] Cannot create a file when that file already exists:
    'C:\\Users\\...\\Desktop\\Conrod-win64\\Conrod-update'

The old code was two statements that disagreed with each other:

    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

The first says the folder might refuse to go. The second insists it did.
People install this on their Desktop, Desktops live in OneDrive, and the
sync client holds handles open -- so the delete failed quietly and the
mkdir raised, every time, with no way forward but deleting the folder by
hand.

Stepping aside to a fresh folder costs some disk until the next attempt
tidies it. Refusing to update costs the update.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conrod import update


class ClearingSomewhereToUnpack(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.beside = Path(tmp.name)

    def test_the_usual_folder_when_nothing_is_in_the_way(self) -> None:
        got = update._clear_staging(self.beside)
        self.assertEqual(got.name, "Conrod-update")
        self.assertTrue(got.is_dir())

    def test_a_leftover_folder_is_cleared_out(self) -> None:
        stale = self.beside / "Conrod-update"
        (stale / "Conrod").mkdir(parents=True)
        (stale / "Conrod" / "old.txt").write_text("last time", encoding="utf-8")

        got = update._clear_staging(self.beside)
        self.assertEqual(got.name, "Conrod-update")
        self.assertEqual(list(got.iterdir()), [],
                         "must unpack into an empty folder, not onto old files")

    def test_a_folder_that_will_not_go_is_stepped_around(self) -> None:
        """OneDrive holding the old folder open is the reported case, and it
        must not be the end of the update."""
        stale = self.beside / "Conrod-update"
        stale.mkdir()
        with patch.object(update.shutil, "rmtree", lambda *a, **k: None):
            got = update._clear_staging(self.beside)
        self.assertNotEqual(got.name, "Conrod-update")
        self.assertTrue(got.is_dir())
        self.assertTrue(stale.is_dir(), "the locked one is left alone")

    def test_it_keeps_stepping_while_folders_are_held(self) -> None:
        for name in ("Conrod-update", "Conrod-update-2", "Conrod-update-3"):
            (self.beside / name).mkdir()
        with patch.object(update.shutil, "rmtree", lambda *a, **k: None):
            got = update._clear_staging(self.beside)
        self.assertEqual(got.name, "Conrod-update-4")

    def test_it_gives_up_with_a_sentence_someone_can_act_on(self) -> None:
        """Not WinError 183. The reader has to know what to close."""
        for n in range(1, 13):
            name = "Conrod-update" if n == 1 else f"Conrod-update-{n}"
            (self.beside / name).mkdir()
        with patch.object(update.shutil, "rmtree", lambda *a, **k: None):
            with self.assertRaises(RuntimeError) as caught:
                update._clear_staging(self.beside)
        why = str(caught.exception)
        self.assertIn("OneDrive", why)
        self.assertIn("Conrod-update", why)

    def test_install_uses_it(self) -> None:
        import inspect

        source = inspect.getsource(update.install)
        self.assertIn("_clear_staging", source)
        self.assertNotIn("ignore_errors=True", source)


if __name__ == "__main__":
    unittest.main()
