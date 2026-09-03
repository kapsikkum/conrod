"""Cameras and bursts, including the two-shooter case that motivates both."""

from __future__ import annotations

import unittest

from conrod import bursts


def _row(path, *, serial=None, model="Canon EOS R5", lens=None,
         stamp=None, subsec=None):
    row = {"SourceFile": path, "Model": model}
    if serial:
        row["SerialNumber"] = serial
    if lens:
        row["LensModel"] = lens
    if stamp:
        row["DateTimeOriginal"] = stamp
    if subsec:
        row["SubSecTimeOriginal"] = subsec
    return row


class Cameras(unittest.TestCase):
    def test_the_serial_separates_two_identical_bodies(self) -> None:
        """The whole reason serial beats model: same camera, two shooters."""
        a = bursts.camera_of(_row("a.cr3", serial="0123"))
        b = bursts.camera_of(_row("b.cr3", serial="9876"))
        self.assertNotEqual(a, b)

    def test_the_lens_stands_in_when_there_is_no_serial(self) -> None:
        a = bursts.camera_of(_row("a.cr3", lens="RF 70-200mm"))
        b = bursts.camera_of(_row("b.cr3", lens="RF 24-70mm"))
        self.assertNotEqual(a, b)

    def test_a_file_with_nothing_useful_falls_back(self) -> None:
        self.assertEqual(bursts.camera_of({}, fallback="Sunday"), "Sunday")

    def test_the_same_body_is_the_same_camera(self) -> None:
        self.assertEqual(bursts.camera_of(_row("a.cr3", serial="0123")),
                         bursts.camera_of(_row("b.cr3", serial="0123")))


class Timestamps(unittest.TestCase):
    def test_subseconds_are_kept(self) -> None:
        """Ten frames a second all stamp the same whole second.

        Without the sub-second field a burst is one indivisible lump and the
        gap between two cars cannot be seen at all.
        """
        early = bursts.taken_at(_row("a", stamp="2026:08:22 11:04:07",
                                     subsec="10"))
        late = bursts.taken_at(_row("b", stamp="2026:08:22 11:04:07",
                                    subsec="90"))
        self.assertLess(early, late)

    def test_a_flat_clock_battery_is_unknown_not_an_error(self) -> None:
        self.assertIsNone(bursts.taken_at(_row("a", stamp="0000:00:00 00:00:00")))

    def test_no_timestamp_at_all(self) -> None:
        self.assertIsNone(bursts.taken_at({}))

    def test_a_timezone_suffix_does_not_break_it(self) -> None:
        self.assertIsNotNone(
            bursts.taken_at(_row("a", stamp="2026:08:22 11:04:07+10:00")))


class Bursts(unittest.TestCase):
    def test_a_gap_between_cars_starts_a_new_burst(self) -> None:
        rows = [_row("1", serial="A", stamp="2026:08:22 11:04:07", subsec="10"),
                _row("2", serial="A", stamp="2026:08:22 11:04:07", subsec="60"),
                _row("3", serial="A", stamp="2026:08:22 11:04:08", subsec="10"),
                # twenty seconds later: the next car
                _row("4", serial="A", stamp="2026:08:22 11:04:28", subsec="10"),
                _row("5", serial="A", stamp="2026:08:22 11:04:28", subsec="60")]
        frames = bursts.describe(rows)
        keys = [f.burst for f in frames]
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], keys[2])
        self.assertNotEqual(keys[2], keys[3])
        self.assertEqual(keys[3], keys[4])

    def test_two_shooters_never_share_a_burst(self) -> None:
        """The case filenames and timestamps alone both get wrong.

        Both bodies fire at the same car within the same second. Grouped on
        time alone that is one burst, and it is two -- two angles, two
        exposures, two sets of frames that should be reasoned about apart.
        """
        rows = [_row("a1", serial="AAA", stamp="2026:08:22 11:04:07", subsec="10"),
                _row("b1", serial="BBB", stamp="2026:08:22 11:04:07", subsec="20"),
                _row("a2", serial="AAA", stamp="2026:08:22 11:04:07", subsec="30"),
                _row("b2", serial="BBB", stamp="2026:08:22 11:04:07", subsec="40")]
        frames = {f.path: f for f in bursts.describe(rows)}
        self.assertEqual(frames["a1"].burst, frames["a2"].burst)
        self.assertEqual(frames["b1"].burst, frames["b2"].burst)
        self.assertNotEqual(frames["a1"].burst, frames["b1"].burst)

    def test_frames_out_of_order_still_burst_correctly(self) -> None:
        """Filenames do not order a shoot; timestamps do."""
        rows = [_row("late", serial="A", stamp="2026:08:22 11:04:28", subsec="10"),
                _row("early", serial="A", stamp="2026:08:22 11:04:07", subsec="10"),
                _row("mid", serial="A", stamp="2026:08:22 11:04:07", subsec="60")]
        frames = {f.path: f for f in bursts.describe(rows)}
        self.assertEqual(frames["early"].burst, frames["mid"].burst)
        self.assertNotEqual(frames["early"].burst, frames["late"].burst)

    def test_an_untimed_frame_gets_its_own_burst(self) -> None:
        """A missing clock is not evidence of belonging to anything."""
        rows = [_row("1", serial="A", stamp="2026:08:22 11:04:07", subsec="10"),
                _row("2", serial="A"),
                _row("3", serial="A")]
        frames = {f.path: f for f in bursts.describe(rows)}
        self.assertEqual(len({f.burst for f in frames.values()}), 3)

    def test_collect_returns_bursts_in_the_order_they_were_shot(self) -> None:
        rows = [_row("late", serial="A", stamp="2026:08:22 11:09:00", subsec="10"),
                _row("early", serial="A", stamp="2026:08:22 11:04:07", subsec="10")]
        collected = bursts.collect(bursts.describe(rows))
        self.assertEqual([b.frames[0] for b in collected], ["early", "late"])

    def test_a_burst_knows_its_camera_and_span(self) -> None:
        rows = [_row("1", serial="A", stamp="2026:08:22 11:04:07", subsec="10"),
                _row("2", serial="A", stamp="2026:08:22 11:04:08", subsec="10")]
        burst = bursts.collect(bursts.describe(rows))[0]
        self.assertEqual(len(burst), 2)
        self.assertIn("A", burst.camera)
        self.assertAlmostEqual(burst.ended - burst.started, 1.0, places=2)


class Storing(unittest.TestCase):
    """Writing the burst back onto the frames it was computed for."""

    def test_paths_match_however_they_are_spelled(self) -> None:
        """The bug that made every burst useless, silently.

        exiftool reports SourceFile with forward slashes; the database holds
        what Windows gave us. Comparing the strings matched nothing, and an
        UPDATE that hits no rows is not an error, so ninety-one correctly
        detected bursts were computed and thrown away without a word.
        """
        import sqlite3
        from conrod import store

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE images (id INTEGER PRIMARY KEY, job_id INT, path TEXT,"
            " camera TEXT, burst_key INT, taken_at REAL);")
        conn.execute("INSERT INTO images (id, job_id, path) VALUES (1, 5, ?)",
                     (r"D:\Work\2026\08\IMG_0001.CR3",))

        frame = bursts.Frame(path="D:/Work/2026/08/IMG_0001.CR3",
                             camera="Canon EOS R7 123", taken=100.0, burst=7)
        self.assertEqual(store.set_frame_origin(conn, 5, [frame]), 1)
        row = conn.execute("SELECT camera, burst_key FROM images").fetchone()
        self.assertEqual(row["burst_key"], 7)
        self.assertEqual(row["camera"], "Canon EOS R7 123")

    def test_a_frame_the_job_never_had_is_ignored(self) -> None:
        import sqlite3
        from conrod import store

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE images (id INTEGER PRIMARY KEY, job_id INT, path TEXT,"
            " camera TEXT, burst_key INT, taken_at REAL);")
        frame = bursts.Frame(path="D:/elsewhere.CR3", camera="x", taken=1.0, burst=1)
        self.assertEqual(store.set_frame_origin(conn, 5, [frame]), 0)


if __name__ == "__main__":
    unittest.main()
