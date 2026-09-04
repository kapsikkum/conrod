"""Remembering what a plate turned out to be.

The same cars come back to the same meets, and the vision model disagrees
with itself about a fifth of the frames of a single burst -- so a plate is
worth more than another guess at the same car. Once a plate has been
identified, what it turned out to be is kept against it, and the next album
that reads the plate starts from that answer.

The two rules are the design, and both are about being wrong safely.

**Blanks only.** What was read off the frame in front of it always wins. A
registry allowed to overrule the frame would be confidently wrong the first
time a car was resprayed or a plate moved to another shell, and wrong
silently, everywhere, for as long as the row survived.

**Exact plates only**, once case and punctuation are gone. Grouping may join
43111J to 73111J inside one burst because it has other evidence there -- same
crop, same burst, seconds apart -- and the cost of being wrong is one pile.
Here there is no other evidence and the cost is another car's identity
written onto this one, so a misread simply misses.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conrod import registry, store
from conrod.analyze import VehicleAnalysis


class _Registry(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "conrod.db")
        self.addCleanup(self.conn.close)

    def _know(self, plate, **fields):
        car = VehicleAnalysis(plate=plate)
        for key, value in fields.items():
            setattr(car, key, value)
        registry.remember(self.conn, car)
        return car


class TheKeyIsThePlate(_Registry):
    def test_punctuation_and_case_are_noise(self) -> None:
        """One plate written three ways is one car."""
        for written in ("39432J", "39432-j", " 39432 J "):
            self.assertEqual(registry.normalise(written), "39432J")

    def test_no_character_is_ever_corrected(self) -> None:
        """A misread plate must miss rather than match somebody else."""
        self.assertNotEqual(registry.normalise("43111J"),
                            registry.normalise("73111J"))

    def test_a_near_miss_finds_nothing(self) -> None:
        self._know("43111J", make="Ford", model="Falcon")
        known = registry.load(self.conn)
        car = VehicleAnalysis(plate="73111J")
        self.assertFalse(registry.fill(car, known))
        self.assertIsNone(car.make)

    def test_nothing_is_remembered_without_a_plate(self) -> None:
        self.assertFalse(registry.remember(
            self.conn, VehicleAnalysis(make="Ford")))
        self.assertEqual(registry.count(self.conn), 0)


class ItOnlyFillsBlanks(_Registry):
    def setUp(self) -> None:
        super().setUp()
        self._know("39432J", make="Ford", model="Falcon FG XR6",
                   colour="orange", body_type="ute")
        self.known = registry.load(self.conn)

    def test_a_car_nothing_was_read_off_is_filled_in(self) -> None:
        car = VehicleAnalysis(plate="39432J")
        self.assertTrue(registry.fill(car, self.known))
        self.assertEqual(car.make, "Ford")
        self.assertEqual(car.model, "Falcon FG XR6")
        self.assertEqual(car.colour, "orange")

    def test_what_was_read_today_wins(self) -> None:
        """The car may have been resprayed. The frame in front of it is the
        evidence; this is a memory of another day."""
        car = VehicleAnalysis(plate="39432J", colour="red")
        registry.fill(car, self.known)
        self.assertEqual(car.colour, "red")
        self.assertEqual(car.make, "Ford")      # the blank was still filled

    def test_an_empty_list_counts_as_blank(self) -> None:
        self._know("ABC123", sponsors="Hoppy Pumps, Castrol")
        known = registry.load(self.conn)
        car = VehicleAnalysis(plate="ABC123")
        registry.fill(car, known)
        self.assertEqual(car.sponsors, ["Hoppy Pumps", "Castrol"])

    def test_sponsors_read_today_are_not_replaced(self) -> None:
        self._know("ABC123", sponsors="Castrol")
        known = registry.load(self.conn)
        car = VehicleAnalysis(plate="ABC123", sponsors=["Shell"])
        registry.fill(car, known)
        self.assertEqual(car.sponsors, ["Shell"])

    def test_an_unknown_plate_changes_nothing(self) -> None:
        car = VehicleAnalysis(plate="NOTHERE")
        self.assertFalse(registry.fill(car, self.known))

    def test_no_registry_at_all_is_not_an_error(self) -> None:
        car = VehicleAnalysis(plate="39432J")
        self.assertFalse(registry.fill(car, {}))


class ItWritesItselfAsCarsAppear(_Registry):
    def test_a_car_identified_today_is_known_tomorrow(self) -> None:
        self._know("39432J", make="Ford", colour="orange")
        known = registry.load(self.conn)
        self.assertIn("39432J", known)
        self.assertEqual(known["39432J"]["make"], "Ford")

    def test_a_later_album_fills_gaps_the_first_one_left(self) -> None:
        self._know("39432J", make="Ford")
        self._know("39432J", colour="orange")
        known = registry.load(self.conn)
        self.assertEqual(known["39432J"]["make"], "Ford")
        self.assertEqual(known["39432J"]["colour"], "orange")

    def test_a_later_album_cannot_blank_what_it_did_not_see(self) -> None:
        """A frame where no team was visible does not mean the car has no
        team. It means that photograph did not show one."""
        self._know("39432J", make="Ford", team="CV Performance")
        self._know("39432J", make="Ford")
        known = registry.load(self.conn)
        self.assertEqual(known["39432J"]["team"], "CV Performance")

    def test_a_plate_with_nothing_read_off_it_is_not_stored(self) -> None:
        """A row of empty columns is not knowledge."""
        registry.remember(self.conn, VehicleAnalysis(plate="EMPTY1"))
        self.assertEqual(registry.count(self.conn), 0)


class SeedingFromAlbumsAlreadyDone(_Registry):
    def _identified(self, plate, attributes):
        job = store.create_job(self.conn, Path("C:/shoot"), "meet", {})
        path = Path(f"C:/shoot/{plate}.CR3")
        store.add_images(self.conn, job, [path])
        image_id = self.conn.execute(
            "SELECT id FROM images WHERE path=?", (str(path),)).fetchone()[0]
        det = store.add_detection(self.conn, image_id, (0, 0, 10, 10),
                                  "car", 0.9, "C:/crops/00.jpg")
        self.conn.execute(
            "UPDATE detections SET plate=?, attributes=? WHERE id=?",
            (plate, attributes, det))
        self.conn.commit()

    def test_it_reads_what_identification_already_worked_out(self) -> None:
        """Otherwise the registry starts empty on a machine that has been
        shooting for months, with every answer already in the database."""
        self._identified("39432J", '{"make": "Ford", "colour": "orange"}')
        out = registry.seed(self.conn)
        self.assertEqual(out["written"], 1)
        self.assertEqual(registry.load(self.conn)["39432J"]["make"], "Ford")

    def test_it_can_be_run_twice(self) -> None:
        self._identified("39432J", '{"make": "Ford"}')
        registry.seed(self.conn)
        again = registry.seed(self.conn)
        self.assertEqual(again["written"], 0)
        self.assertEqual(registry.count(self.conn), 1)

    def test_a_detection_with_no_plate_is_skipped(self) -> None:
        self._identified("", '{"make": "Ford"}')
        self.assertEqual(registry.seed(self.conn)["written"], 0)

    def test_unreadable_stored_json_does_not_stop_it(self) -> None:
        self._identified("39432J", "{not json")
        self._identified("ABC123", '{"make": "Holden"}')
        self.assertEqual(registry.seed(self.conn)["written"], 1)


class EditingItSomewhereElse(_Registry):
    def test_a_round_trip_keeps_everything(self) -> None:
        self._know("39432J", make="Ford", model="Falcon FG XR6",
                   colour="orange", sponsors="Hoppy Pumps")
        text = registry.to_csv(self.conn)
        self.assertIn("plate,make,model", text)
        self.assertIn("39432J", text)
        self.assertIn("Hoppy Pumps", text)

        fresh = store.connect(Path(tempfile.mkdtemp()) / "other.db")
        self.addCleanup(fresh.close)
        registry.from_csv(fresh, text)
        self.assertEqual(registry.load(fresh)["39432J"]["model"],
                         "Falcon FG XR6")

    def test_what_is_typed_in_the_file_wins(self) -> None:
        """The opposite rule to remember(), and deliberately: this is
        somebody sitting down to correct the registry, which is the one case
        where the new value should replace the old."""
        self._know("39432J", make="Ford", model="Falcon")
        registry.from_csv(self.conn, "plate,model\n39432J,Falcon FG XR6\n")
        self.assertEqual(registry.load(self.conn)["39432J"]["model"],
                         "Falcon FG XR6")

    def test_a_blank_cell_is_not_an_instruction_to_erase(self) -> None:
        """So a file with only the columns you care about is a usable edit."""
        self._know("39432J", make="Ford", colour="orange")
        registry.from_csv(self.conn, "plate,model\n39432J,Falcon FG XR6\n")
        known = registry.load(self.conn)
        self.assertEqual(known["39432J"]["colour"], "orange")
        self.assertEqual(known["39432J"]["make"], "Ford")

    def test_a_file_with_no_plate_column_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            registry.from_csv(self.conn, "make,model\nFord,Falcon\n")

    def test_rows_with_no_plate_are_counted_not_guessed_at(self) -> None:
        out = registry.from_csv(
            self.conn, "plate,make\n,Ford\n39432J,Holden\n")
        self.assertEqual(out["written"], 1)
        self.assertEqual(out["skipped"], 1)

    def test_plates_in_a_file_are_normalised_like_any_other(self) -> None:
        registry.from_csv(self.conn, "plate,make\n39432-j,Ford\n")
        self.assertIn("39432J", registry.load(self.conn))


class WhereItSitsInTheReading(unittest.TestCase):
    def test_it_is_consulted_after_every_reader(self) -> None:
        """Last, so that everything read off the actual frame has already
        had its say by the time the memory is allowed to fill anything."""
        import inspect

        from conrod import analyze as analyze_mod

        source = inspect.getsource(analyze_mod.analyze)
        self.assertGreater(source.index("registry.fill"),
                           source.index("vlm.describe"))

    def test_the_scan_writes_back_as_it_goes(self) -> None:
        import inspect

        from conrod import pipeline

        source = inspect.getsource(pipeline._analysis_worker)
        self.assertIn("registry.remember", source)
        self.assertIn("registry.load", source)

    def test_it_can_be_turned_off(self) -> None:
        from conrod.config import Settings

        self.assertTrue(Settings().use_known_vehicles)


if __name__ == "__main__":
    unittest.main()
