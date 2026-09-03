"""Reconciling a group's disagreeing readings into one name.

The model is allowed to choose and tidy. It is not allowed to know things.
Most of what is tested here is the second half.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from conrod import normalise
from conrod.config import Settings


def _answer(make=None, model=None, colour=None, confident=True):
    """Stand in for Ollama returning a structured answer."""
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            import json as _json
            return {"response": _json.dumps(
                {"make": make, "model": model, "colour": colour,
                 "confident": confident})}

    class Client:
        def post(self, *a, **k):
            return Response()

        def close(self):
            pass

    return Client()


def _run(readings, **answer):
    with patch("httpx.Client", lambda *a, **k: _answer(**answer)):
        return normalise.canonical(readings, Settings())


class Tidying(unittest.TestCase):
    def test_joins_details_across_readings(self) -> None:
        """The whole point: one reading has the nameplate, another the series."""
        out = _run(["Ford Falcon", "Falcon FG", "Ford Fairmont"],
                   make="Ford", model="Falcon FG")
        self.assertEqual(out.make, "Ford")
        self.assertEqual(out.model, "Falcon FG")

    def test_strips_the_make_out_of_the_model(self) -> None:
        out = _run(["Ford Falcon", "Falcon FG"], make="Ford",
                   model="Ford Falcon FG")
        self.assertEqual(out.model, "Falcon FG")

    def test_a_lone_reading_is_left_alone(self) -> None:
        """Nothing to reconcile, so nothing to embellish. No call is made."""
        def explode(*a, **k):
            raise AssertionError("asked the model about a single reading")

        with patch("httpx.Client", explode):
            out = normalise.canonical(["Ford Falcon"], Settings())
        self.assertFalse(out)

    def test_no_readings_at_all(self) -> None:
        self.assertFalse(normalise.canonical([], Settings()))


class Invention(unittest.TestCase):
    """Everything the model returns has to be traceable to a reading."""

    def test_a_make_nobody_saw_is_dropped(self) -> None:
        out = _run(["silver SUV", "grey wagon"], make="Yamaha",
                   model="YZF-R1")
        self.assertIsNone(out.make)
        self.assertIsNone(out.model)
        self.assertIn("Yamaha", out.rejected)

    def test_an_invented_series_is_trimmed_off(self) -> None:
        """"MkII" was read nowhere, so it goes -- but "Falcon FG" stays."""
        out = _run(["Ford Falcon", "Falcon FG"], make="Ford",
                   model="Falcon FG MkII")
        self.assertEqual(out.make, "Ford")
        self.assertEqual(out.model, "Falcon FG")
        self.assertIn("Falcon FG MkII", out.rejected)

    def test_the_nameplate_survives_an_invented_series(self) -> None:
        """The Commodore case: every frame saw the nameplate, none saw "VE"."""
        out = _run(["Commodore", "Commodore VC", "VX Commodore"],
                   make="Holden", model="Commodore VE")
        self.assertEqual(out.model, "Commodore")

    def test_the_spelling_that_was_seen_is_the_spelling_returned(self) -> None:
        """From a real session: "X-Trail" came back as "X Trail".

        Splitting on the hyphen to check each word was read is fine.
        Rejoining with a space is not -- the tidied name then matched none of
        the other X-Trails and one car became three groups.
        """
        out = _run(["Nissan X-Trail", "Nissan X-Trail SUV"],
                   make="Nissan", model="X Trail")
        self.assertEqual(out.model, "X-Trail")

    def test_a_make_implied_by_a_nameplate_is_allowed(self) -> None:
        """No reading says Ford, but only Ford makes a Falcon."""
        out = _run(["Falcon FG", "FG Falcon XR6"], make="Ford",
                   model="Falcon FG")
        self.assertEqual(out.make, "Ford")

    def test_a_model_keeps_its_own_make(self) -> None:
        """A bare nameplate still gets the marque marques.py knows about."""
        out = _run(["Ninja H2", "Kawasaki Ninja"], make=None, model="Ninja H2")
        self.assertEqual(out.model, "Ninja H2")
        self.assertEqual(out.make, "Kawasaki")


class Majority(unittest.TestCase):
    """How often a reading was seen is the evidence, and it was being lost."""

    def test_a_clear_majority_settles_it_without_asking(self) -> None:
        """The real Jaguar, from a real burst of twenty frames.

        Nine frames read "Jaguar XJ-S", eight "Jaguar XJS", two "Nissan
        Fairlady Z" and one "Nissan 240Z". Deduplicating the readings and
        handing the model a plain bullet list threw the counts away, so it
        saw three equal-looking options and picked the Nissan.
        """
        members = ([{"own_make": "Jaguar", "own_model": "XJ-S"}] * 9
                   + [{"own_make": "Jaguar", "own_model": "XJS"}] * 8
                   + [{"own_make": "Nissan", "own_model": "Fairlady Z"}] * 2
                   + [{"own_make": "Nissan", "own_model": "240Z"}])
        readings = normalise.readings_of(members)
        self.assertEqual(readings[0].count, 17)      # both spellings, one car

        def explode(*a, **k):
            raise AssertionError("asked the model to arbitrate a 17-to-3 majority")

        with patch("httpx.Client", explode):
            out = normalise.canonical(readings, Settings())
        self.assertEqual(out.make, "Jaguar")
        self.assertEqual(out.model, "XJ-S")

    def test_a_genuine_split_still_goes_to_the_model(self) -> None:
        members = ([{"own_make": "Ford", "own_model": "Fairmont"}] * 5
                   + [{"own_make": "Ford", "own_model": "Fiesta"}] * 5)
        readings = normalise.readings_of(members)
        with patch("httpx.Client", lambda *a, **k: _answer(make="Ford")):
            out = normalise.canonical(readings, Settings())
        self.assertEqual(out.make, "Ford")

    def test_the_counts_reach_the_prompt(self) -> None:
        """The model cannot weigh evidence it was never shown."""
        seen = {}

        class Spy:
            def post(self, url, json=None, **k):
                seen["prompt"] = json["prompt"]
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {"response": '{"make":null,"model":null,'
                                            '"colour":null,"confident":false}'}
                return R()

            def close(self):
                pass

        members = ([{"own_make": "Ford", "own_model": "Fairmont"}] * 3
                   + [{"own_make": "Ford", "own_model": "Fiesta"}] * 2)
        with patch("httpx.Client", lambda *a, **k: Spy()):
            normalise.canonical(normalise.readings_of(members), Settings())
        self.assertIn("3 of 5", seen["prompt"])
        self.assertIn("2 of 5", seen["prompt"])


class Failure(unittest.TestCase):
    def test_ollama_being_down_is_not_an_error(self) -> None:
        """A scan must survive Ollama being absent. The group keeps its vote."""
        class Dead:
            def post(self, *a, **k):
                raise OSError("connection refused")

            def close(self):
                pass

        with patch("httpx.Client", lambda *a, **k: Dead()):
            out = normalise.canonical(["Ford Falcon", "Falcon FG"], Settings())
        self.assertFalse(out)

    def test_a_non_json_reply_is_not_an_error(self) -> None:
        class Babbling:
            def post(self, *a, **k):
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {"response": "Sure! Here you go:"}
                return R()

            def close(self):
                pass

        with patch("httpx.Client", lambda *a, **k: Babbling()):
            out = normalise.canonical(["Ford Falcon", "Falcon FG"], Settings())
        self.assertFalse(out)


class Readings(unittest.TestCase):
    def test_reads_each_frames_own_answer_not_the_group_answer(self) -> None:
        """Voting on a previous round's group answer reinforces bad merges."""
        members = [
            {"own_make": "Ford", "own_model": "Falcon", "make": "Yamaha",
             "model": "YZF-R1"},
            {"own_make": "Ford", "own_model": "Falcon FG", "make": "Yamaha",
             "model": "YZF-R1"},
        ]
        self.assertEqual([r.text for r in normalise.readings_of(members)],
                         ["Ford Falcon", "Ford Falcon FG"])

    def test_the_same_car_spelled_differently_is_one_reading(self) -> None:
        """Straight from a real session, where all three pairs occurred.

        Each is one car written two ways. Treated as a disagreement they
        cost a model call and made a unanimous group look divided.
        """
        for a, b in [("XJS", "XJ-S"), ("Cooper S", "Cooper-S"),
                     ("Hilux", "HiLux"), ("Cooper S", "COOPER S")]:
            with self.subTest(spelling=f"{a} / {b}"):
                readings = normalise.readings_of(
                    [{"make": "X", "model": a}, {"make": "X", "model": b}])
                self.assertEqual(len(readings), 1, readings)

    def test_it_does_not_merge_different_cars(self) -> None:
        """XJ6 is not an XJS, however close the spelling."""
        readings = normalise.readings_of(
            [{"make": "Jaguar", "model": "XJ6"},
             {"make": "Jaguar", "model": "XJS"}])
        self.assertEqual(len(readings), 2)

    def test_the_commonest_spelling_is_the_one_kept(self) -> None:
        readings = normalise.readings_of(
            [{"make": "X", "model": "HiLux"}] * 3 + [{"make": "X", "model": "Hilux"}])
        self.assertEqual([r.text for r in readings], ["X HiLux"])

    def test_identical_readings_collapse(self) -> None:
        members = [{"make": "Ford", "model": "Falcon"}] * 9
        readings = normalise.readings_of(members)
        self.assertEqual([r.text for r in readings], ["Ford Falcon"])
        self.assertEqual(readings[0].count, 9)

    def test_a_group_that_agrees_never_reaches_the_model(self) -> None:
        """Nine frames saying the same thing collapse to one reading."""
        members = [{"make": "Ford", "model": "Falcon"}] * 9

        def explode(*a, **k):
            raise AssertionError("called the model for a group that agreed")

        with patch("httpx.Client", explode):
            out = normalise.canonical(normalise.readings_of(members), Settings())
        self.assertFalse(out)


if __name__ == "__main__":
    unittest.main()
