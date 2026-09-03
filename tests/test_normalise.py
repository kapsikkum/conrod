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
        self.assertEqual(normalise.readings_of(members),
                         ["Ford Falcon", "Ford Falcon FG"])

    def test_identical_readings_collapse(self) -> None:
        members = [{"make": "Ford", "model": "Falcon"}] * 9
        self.assertEqual(normalise.readings_of(members), ["Ford Falcon"])

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
