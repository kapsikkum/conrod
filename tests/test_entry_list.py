"""The entry list: what is loaded, and being able to stop loading it.

A CSV of race numbers to drivers and teams is applied to every scan, so it
has to be obvious which one is in use and possible to take it back out. It
was neither: an entry list uploaded once was reloaded at every launch, shown
nowhere in Settings, and could not be removed from inside the application at
all.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from conrod import server
from conrod.config import Settings
from conrod.mapping import NumberMap


def _csv(folder: Path) -> Path:
    path = folder / "entries.csv"
    path.write_text("number,driver,team\n7,Someone,A Team\n", encoding="utf-8")
    return path


class WhatIsLoaded(unittest.TestCase):
    def test_configure_reports_the_list_it_was_started_with(self) -> None:
        """main.py loads the CSV at startup and hands over the map.

        Nothing passed on *which file*, so an entry list applied to every
        scan was invisible in Settings -- and something you cannot see is
        something you cannot turn off.
        """
        with TemporaryDirectory() as tmp:
            path = _csv(Path(tmp))
            settings = Settings()
            settings.extra["map_path"] = str(path)
            server.configure(settings, NumberMap.load(path))
            self.assertEqual(server._state["map_path"], str(path))

    def test_no_list_reports_none(self) -> None:
        server.configure(Settings(), NumberMap())
        self.assertIsNone(server._state["map_path"])


class ClearingIt(unittest.TestCase):
    """Driven through the real endpoint.

    Settings.save is stubbed: the point is what the handler does to the
    settings it will persist, and a test has no business writing to the
    installed application's data root.
    """

    def _client(self, settings):
        from fastapi.testclient import TestClient
        server.configure(settings, NumberMap())
        return TestClient(server.app)

    def test_clearing_removes_it_from_what_gets_saved(self) -> None:
        """Forgetting it in memory was not enough.

        The path stayed in the saved settings, startup loaded it again, and
        an entry list cleared in the UI came straight back -- so a CSV loaded
        once could never be got rid of.
        """
        with TemporaryDirectory() as tmp:
            settings = Settings()
            settings.extra["map_path"] = str(_csv(Path(tmp)))
            saved = []
            settings.save = lambda: saved.append(dict(settings.extra))

            response = self._client(settings).post(
                "/api/settings", json={"settings": {}, "map_path": ""})
            self.assertEqual(response.status_code, 200)

            self.assertNotIn("map_path", settings.extra)
            self.assertTrue(saved, "the change was never persisted")
            self.assertNotIn("map_path", saved[-1])
            self.assertIsNone(server._state["map_path"])
            self.assertEqual(len(server._state["number_map"]), 0)

    def test_choosing_one_records_it_for_next_time(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _csv(Path(tmp))
            settings = Settings()
            saved = []
            settings.save = lambda: saved.append(dict(settings.extra))

            response = self._client(settings).post(
                "/api/settings", json={"settings": {}, "map_path": str(path)})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(settings.extra.get("map_path"), str(path))
            self.assertEqual(saved[-1].get("map_path"), str(path))
            self.assertEqual(server._state["map_path"], str(path))

    def test_a_csv_that_will_not_read_is_refused(self) -> None:
        settings = Settings()
        settings.save = lambda: None
        response = self._client(settings).post(
            "/api/settings", json={"settings": {}, "map_path": "P:/nope.csv"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
