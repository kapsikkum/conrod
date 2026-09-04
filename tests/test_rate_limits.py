"""A shoot is thousands of crops and every cloud provider meters them, so
429 is part of a normal scan rather than an error in it.

Left alone it was the worst possible outcome: vlm.describe turns any
exception into an empty VehicleDescription, so a rate-limited scan quietly
produced frame after frame with nothing read off it and no sign of why --
the same result as a car nobody could identify.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx

from conrod import vlm_providers
from conrod.config import Settings


def _response(status, headers=None):
    request = httpx.Request("POST", "https://example.test")
    return httpx.Response(status, request=request, headers=headers or {})


class WaitingOutALimit(unittest.TestCase):
    def setUp(self):
        vlm_providers._not_before = 0.0
        self.slept = []
        self._real = time.sleep
        time.sleep = self.slept.append
        self.addCleanup(setattr, time, "sleep", self._real)

    def test_a_429_is_retried_rather_than_raised(self) -> None:
        calls = [_response(429), _response(200)]
        send = MagicMock(side_effect=lambda: calls.pop(0))
        out = vlm_providers._send(send, Settings(), "Anthropic")
        self.assertEqual(out.status_code, 200)
        self.assertEqual(send.call_count, 2)

    def test_the_provider_is_obeyed_when_it_says_how_long(self) -> None:
        calls = [_response(429, {"retry-after": "7"}), _response(200)]
        vlm_providers._send(MagicMock(side_effect=lambda: calls.pop(0)),
                            Settings(), "Anthropic")
        self.assertTrue(any(abs(s - 7) < 0.01 for s in self.slept), self.slept)

    def test_a_guessed_wait_is_never_unbounded(self) -> None:
        """Our own backoff is capped. A provider's retry-after is not.

        These used to be the same rule, and the cap applied to both. That is
        wrong for a stated retry-after: it is a fact about when the limit
        lifts rather than a guess, so clamping five minutes down to one only
        buys another refusal a minute later. Capping still applies to the
        number we invent when the provider says nothing.
        """
        self.assertLessEqual(vlm_providers._retry_after(_response(429), 20),
                             vlm_providers.MAX_WAIT)
        self.assertEqual(
            vlm_providers._retry_after(_response(429, {"retry-after": "300"}), 0),
            300.0)

    def test_a_rate_limit_is_waited_out_rather_than_given_up_on(self) -> None:
        """This used to assert the opposite, and the opposite was wrong.

        Giving up after three refusals meant a provider that was busy for ten
        minutes cost the frame: the call raised, the reader logged a failure,
        and the scan moved on and left that car unnamed -- a hole that
        afterwards is indistinguishable from one the model genuinely could
        not read. The way out of a long limit is Stop, which is heard
        throughout, not a countdown that silently drops work.
        """
        refusals = {"n": 0}

        def send():
            refusals["n"] += 1
            if refusals["n"] <= 20:          # far past any retry budget
                return _response(429)
            return _response(200)

        with patch.object(vlm_providers, "_sleep", lambda s: None):
            out = vlm_providers._send(send, Settings(vlm_max_retries=3),
                                      "Anthropic")
        self.assertEqual(out.status_code, 200)
        self.assertEqual(refusals["n"], 21)

    def test_a_stop_ends_the_wait(self) -> None:
        """Unbounded waiting is only acceptable because this works."""
        vlm_providers.set_stop_check(lambda: True)
        try:
            with self.assertRaises(vlm_providers.Stopped):
                vlm_providers._send(MagicMock(side_effect=lambda: _response(429)),
                                    Settings(), "Anthropic")
        finally:
            vlm_providers.set_stop_check(None)

    def test_a_real_error_is_not_retried(self) -> None:
        """A 400 is the request being wrong. Sending it again four times
        just makes it wrong four more times."""
        send = MagicMock(side_effect=lambda: _response(400))
        with self.assertRaises(httpx.HTTPStatusError):
            vlm_providers._send(send, Settings(), "Anthropic")
        self.assertEqual(send.call_count, 1)

    def test_a_timeout_is_retried_too(self) -> None:
        calls = [httpx.ConnectTimeout("slow"), _response(200)]

        def send():
            item = calls.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        self.assertEqual(vlm_providers._send(send, Settings(), "Gemini").status_code, 200)


class TheWaitIsShared(unittest.TestCase):
    """The analysis pool runs several workers at once. Without a common
    gate each one discovers the limit separately and keeps hammering while
    the others back off, so one 429 has to hold all of them."""

    def setUp(self):
        vlm_providers._not_before = 0.0
        self.addCleanup(setattr, vlm_providers, "_not_before", 0.0)

    def test_one_refusal_holds_the_others(self) -> None:
        vlm_providers._hold_off(30)
        waited = []
        real = time.sleep
        time.sleep = waited.append
        try:
            vlm_providers._wait_turn()
        finally:
            time.sleep = real
        self.assertTrue(waited and waited[0] > 25, waited)

    def test_the_gate_is_not_moved_backwards(self) -> None:
        vlm_providers._hold_off(30)
        vlm_providers._hold_off(1)
        remaining = vlm_providers._not_before - time.monotonic()
        self.assertGreater(remaining, 25)

    def test_it_is_safe_from_several_threads(self) -> None:
        errors = []

        def hammer():
            try:
                for _ in range(200):
                    vlm_providers._hold_off(0)
                    vlm_providers._wait_turn()
            except Exception as exc:      # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(errors, [])


class OllamaIsLeftAlone(unittest.TestCase):
    def test_local_calls_do_not_go_through_the_limiter(self) -> None:
        """Nothing meters a program talking to itself, and a shared backoff
        would make one slow local reply stall every worker."""
        source = vlm_providers.__file__
        from pathlib import Path

        body = Path(source).read_text(encoding="utf-8")
        ollama = body[body.index("def _ollama("):body.index("def _openai(")]
        self.assertNotIn("_send(", ollama)
        self.assertIn("temperature", ollama)


if __name__ == "__main__":
    unittest.main()


class ARateLimitIsWaitedOutNotSkipped(unittest.TestCase):
    """The complaint this was built for: "rate limit shouldn't move on".

    Spending a retry on a 429 means a provider that is busy for ten minutes
    costs the frame -- the call gives up, the reader logs a failure, and the
    scan carries on and leaves that car unnamed. Afterwards that hole is
    indistinguishable from a frame the model genuinely could not read.
    """

    def setUp(self):
        vlm_providers.set_stop_check(None)
        vlm_providers._not_before = 0.0

    def tearDown(self):
        vlm_providers.set_stop_check(None)
        vlm_providers._not_before = 0.0

    @staticmethod
    def _limited(times: int, then=None):
        """A sender refused `times` times, then answering."""
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] <= times:
                response = httpx.Response(
                    429, headers={"retry-after": "0"},
                    request=httpx.Request("POST", "https://example.invalid"))
                raise httpx.HTTPStatusError("429", request=response.request,
                                            response=response)
            return then or httpx.Response(
                200, json={}, request=httpx.Request("POST", "https://example.invalid"))
        return send, calls

    def test_it_keeps_waiting_past_the_retry_budget(self) -> None:
        settings = Settings(vlm_max_retries=2)
        send, calls = self._limited(9)          # far more refusals than retries
        with patch.object(vlm_providers, "_sleep", lambda s: None):
            out = vlm_providers._send(send, settings, "Test")
        self.assertEqual(out.status_code, 200)
        self.assertEqual(calls["n"], 10, "gave up instead of waiting it out")

    def test_a_real_error_still_gives_up(self) -> None:
        """The budget is for things that might actually be broken."""
        settings = Settings(vlm_max_retries=3)

        def send():
            response = httpx.Response(
                500, request=httpx.Request("POST", "https://example.invalid"))
            raise httpx.HTTPStatusError("500", request=response.request,
                                        response=response)
        with patch.object(vlm_providers, "_sleep", lambda s: None):
            with self.assertRaises(httpx.HTTPStatusError):
                vlm_providers._send(send, settings, "Test")

    def test_stop_is_heard_during_a_long_wait(self) -> None:
        """Otherwise Stop does nothing until the provider relents."""
        vlm_providers.set_stop_check(lambda: True)
        with self.assertRaises(vlm_providers.Stopped):
            vlm_providers._sleep(30.0)

    def test_stopped_survives_a_defensive_except_exception(self) -> None:
        """Every reader wraps itself in one, and would swallow this."""
        self.assertNotIsInstance(vlm_providers.Stopped(), Exception)

    def test_a_retry_after_is_obeyed_as_given(self) -> None:
        """It says when the limit lifts. Capping it just asks again too early."""
        response = httpx.Response(429, headers={"retry-after": "300"})
        self.assertEqual(vlm_providers._retry_after(response, 0), 300.0)
