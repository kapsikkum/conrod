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
from unittest.mock import MagicMock

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

    def test_a_wait_is_never_unbounded(self) -> None:
        self.assertLessEqual(
            vlm_providers._retry_after(_response(429, {"retry-after": "99999"}), 0),
            vlm_providers.MAX_WAIT)
        self.assertLessEqual(vlm_providers._retry_after(_response(429), 20),
                             vlm_providers.MAX_WAIT)

    def test_it_gives_up_eventually_rather_than_scanning_for_ever(self) -> None:
        send = MagicMock(side_effect=lambda: _response(429))
        with self.assertRaises(httpx.HTTPStatusError):
            vlm_providers._send(send, Settings(vlm_max_retries=3), "Anthropic")
        self.assertEqual(send.call_count, 3)

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
