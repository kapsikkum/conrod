"""A watch has to stop when the album it was watching is gone.

Reported as "why is it just scanning over and over and over". The live
state, mid-import of a 6,578-frame shoot:

    {"active": true,
     "folder": "D:\\\\Client Work\\\\BLCC Conrod Flyer",
     "job_id": 13,               <- deleted; that album was now job 14
     "interval": 60.0,
     "added": 13156,             <- 6,578 twice, still climbing
     "message": "6578 new frames"}

Three things lined up, and each made the next one worse.

`_known_frames` asks which frames the album already holds. For a job that
does not exist that is an empty set, so every file in the folder is new --
all 6,578 of them, on every pass, for ever.

`_stage_for_album` fell through to "all" for an unknown job. So the one case
where the watch had lost track of what it was watching was also the case
where it committed the machine to a full scan, vision model included.

And nothing cleared the watch when its album was deleted, in memory or in
settings.json -- so it also came back on the next launch.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conrod import server, store
from conrod.config import Settings


class _Watching(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "conrod.db")
        self.addCleanup(self.conn.close)
        self.job = store.create_job(self.conn, Path("D:/shoot"), "meet", {})

        patches = [
            patch.object(server.store, "connect", lambda *a, **k: self.conn),
            patch.object(server.store, "session", lambda: _Session(self.conn)),
            patch.dict(server._state, {"settings": Settings()}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class KnowingWhetherTheAlbumIsStillThere(_Watching):
    def test_an_album_that_exists_is_recognised(self) -> None:
        self.assertTrue(server._album_exists(self.job))

    def test_a_deleted_one_is_not(self) -> None:
        self.assertFalse(server._album_exists(self.job + 999))

    def test_no_album_at_all_is_not(self) -> None:
        self.assertFalse(server._album_exists(None))

    def test_a_database_it_cannot_read_is_given_the_benefit(self) -> None:
        """Not knowing is not proof the album is gone, and stopping a watch
        over a locked database is worse than one wasted pass."""
        def boom(*_a, **_k):
            raise RuntimeError("database is locked")

        with patch.object(server.store, "connect", boom):
            self.assertTrue(server._album_exists(self.job))


class HowFarAWatchedAlbumIsTaken(_Watching):
    def test_an_unknown_album_gets_the_cheapest_answer(self) -> None:
        """It used to get the most expensive one. The watch had lost track
        of what it was watching, and that was the case in which it launched
        a full scan of the whole folder once a minute."""
        self.assertEqual(server._stage_for_album(self.job + 999), "index")

    def test_an_unreadable_database_does_too(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("database is locked")

        with patch.object(server.store, "connect", boom):
            self.assertEqual(server._stage_for_album(self.job), "index")

    def test_an_indexed_album_stays_indexed(self) -> None:
        """A watch does not get to undo staging the work: new frames of an
        album nobody has culled must not go through the vision model."""
        store.set_job_status(self.conn, self.job, "indexed")
        self.assertEqual(server._stage_for_album(self.job), "index")

    def test_a_culled_album_is_culled(self) -> None:
        store.set_job_status(self.conn, self.job, "culled")
        self.assertEqual(server._stage_for_album(self.job), "cull")


class DeletingTheAlbumStopsTheWatch(_Watching):
    def _watching(self):
        server._watch.update({"active": True, "job_id": self.job,
                              "generation": 1, "folder": "D:/shoot"})
        settings: Settings = server._state["settings"]
        settings.extra["watch"] = {"path": "D:/shoot", "job_id": self.job,
                                   "recursive": True, "interval": 60.0}

    def test_forgetting_an_album_forgets_its_watch(self) -> None:
        self._watching()
        with patch.object(Settings, "save", lambda self: None):
            server.delete_job(self.job)
        self.assertFalse(server._watch["active"])

    def test_and_takes_it_out_of_the_settings_file(self) -> None:
        """Otherwise _restore_watch reads it on the next launch and the same
        minute-by-minute rescan starts again."""
        self._watching()
        with patch.object(Settings, "save", lambda self: None):
            server.delete_job(self.job)
        self.assertNotIn("watch", server._state["settings"].extra)

    def test_a_watch_on_a_different_album_is_left_alone(self) -> None:
        other = store.create_job(self.conn, Path("D:/other"), "other", {})
        self._watching()
        with patch.object(Settings, "save", lambda self: None):
            server.delete_job(other)
        self.assertTrue(server._watch["active"])

    def tearDown(self) -> None:
        server._watch.update({"active": False, "job_id": None})


class TheLoopChecksBeforeItPolls(unittest.TestCase):
    def test_it_gives_up_rather_than_polling_a_dead_album(self) -> None:
        """The guard has to come before the poll: polling is what returns
        6,578 "new" frames for an album that no longer holds any."""
        import inspect

        source = inspect.getsource(server._watch_loop)
        self.assertLess(source.index("_album_exists"), source.index("poll("))
        self.assertIn("_forget_watch_setting", source)


class _Session:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_exc):
        self.conn.commit()
        return False


if __name__ == "__main__":
    unittest.main()
