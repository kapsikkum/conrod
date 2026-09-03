"""Watching a folder, and specifically not scanning a half-copied file.

The clock is passed in rather than slept through: a test that waits twenty
seconds to prove a twenty second rule is a test nobody runs.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from conrod import watch


def _write(folder: Path, name: str, size: int = 32) -> Path:
    path = folder / name
    path.write_bytes(b"x" * size)
    return path


class Settling(unittest.TestCase):
    """A file is only offered once it has stopped being written to."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_file_still_growing_is_never_offered(self) -> None:
        """The case the module exists for.

        A 60MB CR3 mid-copy is a real file with a real name and a truncated
        body. Scanning it records a broken frame that no later pass retries,
        so the frame is lost for the price of being early.
        """
        w = watch.Watcher(self.folder, settle=20.0)
        path = _write(self.folder, "IMG_0001.CR3", 10)
        self.assertEqual(w.poll(now=0), [])            # first sighting

        for tick in range(1, 6):                       # copy still running
            _write(self.folder, "IMG_0001.CR3", 10 + tick * 1000)
            self.assertEqual(w.poll(now=tick * 10), [],
                             "a file that changed size was offered anyway")

        # Copy finishes; the clock restarts from the last write.
        self.assertEqual(w.poll(now=55), [])
        self.assertEqual(w.poll(now=90), [path])

    def test_a_finished_file_still_waits_one_interval(self) -> None:
        _write(self.folder, "a.jpg")
        w = watch.Watcher(self.folder, settle=20.0)
        self.assertEqual(w.poll(now=0), [])
        self.assertEqual(w.poll(now=5), [])
        self.assertEqual(len(w.poll(now=100)), 1)

    def test_it_is_not_offered_twice(self) -> None:
        path = _write(self.folder, "a.jpg")
        w = watch.Watcher(self.folder, settle=1.0)
        w.poll(now=0)
        self.assertEqual(w.poll(now=50), [path])
        # The album now holds it, which is what stops the repeat.
        self.assertEqual(w.poll(now=60, known={watch.key(path)}), [])


class WhatCounts(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _settled(self, w, **kw):
        w.poll(now=0)
        return w.poll(now=1000, **kw)

    def test_only_images(self) -> None:
        _write(self.folder, "a.jpg")
        _write(self.folder, "notes.txt")
        _write(self.folder, "clip.mp4")
        out = self._settled(watch.Watcher(self.folder))
        self.assertEqual([p.name for p in out], ["a.jpg"])

    def test_frames_already_in_the_album_are_left_alone(self) -> None:
        """Restarting Conrod must not re-offer the whole folder."""
        old = _write(self.folder, "old.jpg")
        new = _write(self.folder, "new.jpg")
        out = self._settled(watch.Watcher(self.folder), known={watch.key(old)})
        self.assertEqual(out, [new])

    def test_a_path_spelled_differently_is_the_same_file(self) -> None:
        """The mistake that silently threw away every burst once before."""
        path = _write(self.folder, "IMG_1.JPG")
        odd = str(path).replace("\\", "/")
        out = self._settled(watch.Watcher(self.folder), known={watch.key(odd)})
        self.assertEqual(out, [], "the same file under two spellings was rescanned")

    def test_subfolders_are_included_unless_told_otherwise(self) -> None:
        (self.folder / "sunday").mkdir()
        _write(self.folder / "sunday", "a.jpg")
        self.assertEqual(len(self._settled(watch.Watcher(self.folder))), 1)
        self.assertEqual(
            self._settled(watch.Watcher(self.folder, recursive=False)), [])

    def test_results_are_in_a_stable_order(self) -> None:
        for name in ("c.jpg", "a.jpg", "b.jpg"):
            _write(self.folder, name)
        out = self._settled(watch.Watcher(self.folder))
        self.assertEqual([p.name for p in out], ["a.jpg", "b.jpg", "c.jpg"])


class Resilience(unittest.TestCase):
    def test_a_folder_that_is_not_there_is_not_an_error(self) -> None:
        """An unplugged card reader must not kill the watch."""
        w = watch.Watcher(Path("P:/no/such/folder"))
        self.assertEqual(w.poll(now=0), [])
        self.assertEqual(w.poll(now=100), [])

    def test_files_that_go_away_are_forgotten(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write(folder, "a.jpg")
            w = watch.Watcher(folder)
            w.poll(now=0)
            self.assertEqual(len(w.seen), 1)
            path.unlink()
            w.poll(now=10)
            self.assertEqual(w.seen, {}, "a moved-out frame was remembered forever")


class Endpoint(unittest.TestCase):
    """Turning a watch on and off through the real API.

    Settings.save is stubbed throughout: a test has no business writing to
    the installed application's settings.json, least of all while a scan is
    using it.
    """

    def _client(self, settings):
        from fastapi.testclient import TestClient
        from conrod import server
        from conrod.mapping import NumberMap
        server.configure(settings, NumberMap())
        return server, TestClient(server.app)

    def _settings(self):
        from conrod.config import Settings
        settings = Settings()
        settings.save = lambda: None
        return settings

    def test_a_watch_is_remembered_so_it_survives_a_restart(self) -> None:
        """The case worth persisting for: the copy outlives the window."""
        with TemporaryDirectory() as tmp:
            settings = self._settings()
            server, client = self._client(settings)
            r = client.post("/api/watch", json={
                "active": True, "path": tmp, "job_id": 4, "interval": 30})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["active"])
            self.assertEqual(settings.extra["watch"]["job_id"], 4)
            client.post("/api/watch", json={"active": False})

    def test_turning_it_off_forgets_it(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self._settings()
            server, client = self._client(settings)
            client.post("/api/watch", json={"active": True, "path": tmp,
                                            "job_id": 4})
            r = client.post("/api/watch", json={"active": False})
            self.assertFalse(r.json()["active"])
            self.assertNotIn("watch", settings.extra)

    def test_a_folder_that_is_not_there_is_refused(self) -> None:
        _, client = self._client(self._settings())
        r = client.post("/api/watch", json={"active": True,
                                            "path": "P:/nope", "job_id": 1})
        self.assertEqual(r.status_code, 400)

    def test_a_watch_without_an_album_is_refused(self) -> None:
        """A watch continues an album; there is nothing to add frames to."""
        with TemporaryDirectory() as tmp:
            _, client = self._client(self._settings())
            r = client.post("/api/watch", json={"active": True, "path": tmp})
            self.assertEqual(r.status_code, 400)

    def test_the_poll_interval_has_a_floor(self) -> None:
        """A one-second watch would re-list the folder forever."""
        with TemporaryDirectory() as tmp:
            settings = self._settings()
            server, client = self._client(settings)
            r = client.post("/api/watch", json={"active": True, "path": tmp,
                                                "job_id": 1, "interval": 0.1})
            self.assertGreaterEqual(r.json()["interval"], 10)
            client.post("/api/watch", json={"active": False})

    def test_a_saved_watch_pointing_nowhere_does_not_stop_startup(self) -> None:
        """The card came out between sessions."""
        settings = self._settings()
        settings.extra["watch"] = {"path": "P:/gone", "job_id": 2}
        server, _ = self._client(settings)          # configure() must not raise
        self.assertFalse(server.watch_status()["active"])


if __name__ == "__main__":
    unittest.main()
