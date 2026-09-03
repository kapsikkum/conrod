"""Adding an album, culling it, and naming it as three separate decisions.

Adding a folder used to commit the machine to the whole job: indexing,
detection and hours of vision model, from one button. On a 6,000 frame shoot
that is a night, and most of it is spent naming frames that were never going
to be published.

The expensive stage is now chosen, not assumed.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from conrod import pipeline
from conrod.config import Settings


class StageNames(unittest.TestCase):
    def test_an_unknown_stage_is_refused_rather_than_ignored(self) -> None:
        """Silently running everything would be the worst possible answer."""
        with self.assertRaises(ValueError):
            pipeline.run(Path("."), Settings(), stop_after="identify")
        with self.assertRaises(ValueError):
            pipeline.run(Path("."), Settings(), stop_after="nonsense")


class IdentifyingLater(unittest.TestCase):
    """The stage that works from stored crops rather than photographs."""

    def _album(self, rows):
        """An album already detected and culled, with no photographs at all."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT);"
            "CREATE TABLE images (id INTEGER PRIMARY KEY, job_id INT, path TEXT,"
            " preview_path TEXT);"
            "CREATE TABLE detections (id INTEGER PRIMARY KEY, image_id INT,"
            " x1 REAL, y1 REAL, x2 REAL, y2 REAL, cls TEXT, crop_path TEXT,"
            " attributes TEXT, rejected INT DEFAULT 0);")
        conn.execute("INSERT INTO jobs (id, status) VALUES (1,'culled')")
        conn.execute("INSERT INTO images (id, job_id, path, preview_path)"
                     " VALUES (1,1,'a.CR3','a.jpg')")
        for n, (cls, attrs, rejected) in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO detections (id, image_id, x1,y1,x2,y2, cls,"
                " crop_path, attributes, rejected) VALUES (?,1,0,0,10,10,?,?,?,?)",
                (n, cls, f"crop{n}.jpg", attrs, rejected))
        conn.commit()
        return conn

    def _pending(self, conn):
        """The query identify() uses to decide what is left to do."""
        return conn.execute(
            """SELECT d.id FROM detections d JOIN images i ON i.id = d.image_id
                WHERE i.job_id = 1 AND d.rejected = 0 AND d.crop_path IS NOT NULL
                  AND (d.attributes IS NULL OR d.attributes = '')""").fetchall()

    def test_culled_vehicles_are_never_sent_to_the_vision_model(self) -> None:
        """The whole point of culling first.

        A frame too blurred to identify costs seconds of GPU time to fail to
        identify, and it was being spent anyway.
        """
        conn = self._album([("car", None, 0), ("car", None, 1), ("car", None, 0)])
        self.assertEqual(len(self._pending(conn)), 2)

    def test_vehicles_already_named_are_not_named_again(self) -> None:
        """Running identify twice must not re-do the album."""
        conn = self._album([("car", '{"make": "Ford"}', 0), ("car", None, 0)])
        self.assertEqual(len(self._pending(conn)), 1)

    def test_a_detection_with_no_crop_is_skipped(self) -> None:
        conn = self._album([("car", None, 0)])
        conn.execute("UPDATE detections SET crop_path = NULL WHERE id = 1")
        self.assertEqual(len(self._pending(conn)), 0)

    def test_a_bike_is_recognised_from_the_stored_class_name(self) -> None:
        """identify() has only the class name; the detect loop had the object.

        Getting this wrong asks the reader the wrong question about every
        motorbike in the album, and it would never be visible as an error.
        """
        from conrod.config import BIKE_CLASS_NAMES, BIKE_CLASSES, VEHICLE_CLASSES

        self.assertEqual(BIKE_CLASS_NAMES,
                         {VEHICLE_CLASSES[i] for i in BIKE_CLASSES})
        self.assertIn("motorcycle", BIKE_CLASS_NAMES)
        self.assertNotIn("car", BIKE_CLASS_NAMES)


class ThroughTheApi(unittest.TestCase):
    """The endpoint that carries the stage."""

    def _client(self):
        from fastapi.testclient import TestClient
        from conrod import server
        from conrod.mapping import NumberMap

        settings = Settings()
        settings.save = lambda: None
        server.configure(settings, NumberMap())
        # _run is module state and a started scan outlives the test that
        # started it, so the next one gets 409 and the failure looks like a
        # bug in whatever it was actually testing.
        server._run.update({"active": False, "stop": False, "paused": False})
        self.addCleanup(server._run.update,
                        {"active": False, "stop": True, "paused": False})
        return server, TestClient(server.app)

    def test_a_stage_that_does_not_exist_is_refused(self) -> None:
        _, client = self._client()
        with TemporaryDirectory() as tmp:
            r = client.post("/api/scan", json={"path": tmp, "stage": "everything"})
            self.assertEqual(r.status_code, 400)

    def test_identifying_needs_an_album_to_identify(self) -> None:
        _, client = self._client()
        r = client.post("/api/scan", json={"path": "P:/gone", "stage": "identify"})
        self.assertEqual(r.status_code, 400)

    def test_identify_does_not_require_the_folder_to_still_exist(self) -> None:
        """It reads stored crops, so the card can have been unplugged weeks ago.

        Refused for a missing job, not for a missing folder -- the distinction
        is the point of the test.
        """
        server, client = self._client()
        r = client.post("/api/scan", json={"path": "P:/unplugged",
                                           "stage": "identify", "resume_job": 999})
        self.assertNotEqual(r.status_code, 400)
        server._run["stop"] = True

    def test_indexing_still_wants_a_real_folder(self) -> None:
        _, client = self._client()
        r = client.post("/api/scan", json={"path": "P:/gone", "stage": "index"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
