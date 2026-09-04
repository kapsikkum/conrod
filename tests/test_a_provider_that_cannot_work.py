"""A provider that will refuse every crop should say so before the run.

Written from a real one. An identify pass over 3,347 vehicles was pointed
at Anthropic with a Claude Code OAuth token, which can list models and
cannot call /v1/messages. The startup check asked for the model list, was
given it, and let the run start. Every crop then came back 404. Each was
recorded as "nothing could be read off this vehicle", the job finished,
and the album came back with 3% of its cars named -- the same shape it
would have had if the model had simply been bad at motorsport.

Three separate things had to be wrong at once for that to happen, and
each of them is a test below:

    the check asked a question the run does not depend on
    the log line threw away the sentence explaining the refusal
    nothing noticed that every single call was failing identically

Nothing here touches the network. The provider is a stub that answers the
way the real one did.
"""

from __future__ import annotations

import unittest

import httpx

from conrod import vlm, vlm_providers
from conrod.config import Settings


def _http_error(status: int, body=None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


class TheCheckAsksWhatTheRunAsks(unittest.TestCase):
    """The credential that started this could read a list and not generate.

    So a check that reads a list proves nothing about the run, however
    green it comes back.
    """

    def setUp(self) -> None:
        self.settings = Settings(vlm_provider="anthropic", vlm_api_key="sk-ant-api03-x",
                                 vlm_model="claude-haiku-4-5-20251001")
        self.real_call = vlm_providers.call
        self.addCleanup(setattr, vlm_providers, "call", self.real_call)

    def test_a_provider_that_refuses_the_real_call_is_caught(self) -> None:
        def refuse(*_args, **_kwargs):
            raise _http_error(404, {"type": "error", "error": {
                "type": "not_found_error", "message": "model: not available"}})

        vlm_providers.call = refuse
        with self.assertRaises(vlm.VLMUnavailable) as caught:
            vlm.check_available(self.settings)
        self.assertIn("not available", str(caught.exception))

    def test_the_trial_carries_a_picture(self) -> None:
        """Half of what can be wrong is only wrong for a request with an
        image in it -- a text-only model, a plan that meters vision
        separately, an endpoint that takes no image block."""
        seen = {}

        def capture(_settings, **kwargs):
            seen.update(kwargs)
            return {}

        vlm_providers.call = capture
        vlm.check_available(self.settings)
        self.assertEqual(len(seen["images"]), 1)
        self.assertTrue(seen["images"][0])

    def test_a_provider_that_answers_lets_the_run_start(self) -> None:
        vlm_providers.call = lambda *a, **k: {"make": "Ford"}
        vlm.check_available(self.settings)          # no raise

    def test_a_strange_answer_is_not_treated_as_a_refusal(self) -> None:
        """What the model says about a blank grey tile is not evidence
        about the album. Refusing to start over it would be the check
        inventing a failure of its own."""
        def nonsense(*_a, **_k):
            raise ValueError("could not parse the reply")

        vlm_providers.call = nonsense
        vlm.check_available(self.settings)          # no raise


class TheReasonIsRepeatedNotSwallowed(unittest.TestCase):
    """"describe failed: HTTP 404", three thousand times.

    The body said which of the possible 404s it was every time; the line
    read one field, which that provider does not use, and printed nothing.
    """

    def test_openai_and_anthropic_say_it_in_message(self) -> None:
        exc = _http_error(404, {"error": {"type": "not_found_error",
                                          "message": "model: nope"}})
        self.assertIn("model: nope", vlm._brief(exc))

    def test_gemini_has_no_type_field_at_all(self) -> None:
        exc = _http_error(404, {"error": {"code": 404, "status": "NOT_FOUND",
                                          "message": "models/x is not found"}})
        self.assertIn("is not found", vlm._brief(exc))

    def test_ollama_puts_a_bare_string_there(self) -> None:
        exc = _http_error(404, {"error": "model 'qwen' not found"})
        self.assertIn("not found", vlm._brief(exc))

    def test_a_body_that_is_not_json_still_reports_something(self) -> None:
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(502, text="upstream connect error", request=request)
        exc = httpx.HTTPStatusError("x", request=request, response=response)
        self.assertIn("upstream", vlm._brief(exc))

    def test_the_line_names_the_provider_and_the_model(self) -> None:
        """Four providers and one message. Which one refused, and which
        model it was asked for, are the two things worth knowing."""
        written = []
        real = vlm_providers._note
        vlm_providers._note = written.append
        self.addCleanup(setattr, vlm_providers, "_note", real)

        settings = Settings(vlm_provider="gemini", vlm_model="gemini-9-ultra")
        vlm._failed("describe", settings, _http_error(404, {"error": {
            "message": "models/gemini-9-ultra is not found"}}))

        self.assertEqual(len(written), 1)
        self.assertIn("Gemini", written[0])
        self.assertIn("gemini-9-ultra", written[0])
        self.assertIn("is not found", written[0])


class EverythingFailingTheSameWayStopsTheRun(unittest.TestCase):
    """A hole in an album is worse than a run that stopped.

    A stopped run says what to fix and can be run again. Three thousand
    empty descriptions are indistinguishable afterwards from three
    thousand cars the model had nothing to say about, and re-running skips
    them, because they were recorded as answered.
    """

    def setUp(self) -> None:
        vlm_providers.clear_fatal()
        self.addCleanup(vlm_providers.clear_fatal)
        real = vlm_providers._note
        vlm_providers._note = lambda *_a: None
        self.addCleanup(setattr, vlm_providers, "_note", real)
        self.settings = Settings(vlm_provider="anthropic", vlm_model="claude-x")

    def _fail(self, status: int, times: int) -> None:
        for _ in range(times):
            vlm._failed("describe", self.settings,
                        _http_error(status, {"error": {"message": "refused"}}))

    def test_one_refusal_is_just_one_frame(self) -> None:
        """A single 400 can be one crop the provider would not look at.
        Losing the whole scan to that leaves a bigger hole than the frame
        does."""
        self._fail(400, vlm_providers.GIVE_UP_AFTER - 1)
        self.assertEqual(vlm_providers.given_up(), "")

    def test_a_run_of_them_is_the_configuration(self) -> None:
        with self.assertRaises(vlm_providers.Misconfigured) as caught:
            self._fail(404, vlm_providers.GIVE_UP_AFTER)
        why = str(caught.exception)
        self.assertIn("Anthropic", why)
        self.assertIn("refused", why)
        self.assertIn("Settings", why)
        self.assertTrue(vlm_providers.given_up())

    def test_one_success_in_between_clears_it(self) -> None:
        """Cars the model genuinely cannot name are scattered through any
        shoot. Only an unbroken run means the configuration."""
        self._fail(404, vlm_providers.GIVE_UP_AFTER - 1)
        vlm_providers.clear_fatal()
        self._fail(404, vlm_providers.GIVE_UP_AFTER - 1)
        self.assertEqual(vlm_providers.given_up(), "")

    def test_a_rate_limit_never_counts_however_many(self) -> None:
        """429 is the provider saying "not yet", which is waited out
        elsewhere. Counting it here would turn a busy afternoon into a
        stopped scan."""
        self._fail(429, vlm_providers.GIVE_UP_AFTER * 3)
        self.assertEqual(vlm_providers.given_up(), "")

    def test_a_server_fault_never_counts_either(self) -> None:
        self._fail(500, vlm_providers.GIVE_UP_AFTER * 3)
        self.assertEqual(vlm_providers.given_up(), "")

    def test_it_does_not_survive_into_the_next_run(self) -> None:
        with self.assertRaises(vlm_providers.Misconfigured):
            self._fail(404, vlm_providers.GIVE_UP_AFTER)
        vlm_providers.clear_fatal()
        self.assertEqual(vlm_providers.given_up(), "")

    def test_it_is_not_caught_by_the_readers_own_guard(self) -> None:
        """analyze() wraps every reader in `except Exception` so one of
        them failing cannot blank a whole detection. That guard is right,
        and it would have swallowed this exactly as it swallowed the
        404s -- so this is off the Exception branch, like Stopped."""
        self.assertNotIsInstance(vlm_providers.Misconfigured(), Exception)
        self.assertIsInstance(vlm_providers.Misconfigured(), BaseException)


class TheLoopHandingOutCropsStops(unittest.TestCase):
    def test_both_stages_ask_before_queueing_the_next_frame(self) -> None:
        """The workers are the ones that find out, and a worker cannot
        stop the loop that is feeding it."""
        import inspect

        from conrod import pipeline

        for stage in (pipeline.run, pipeline.identify):
            self.assertIn("given_up()", inspect.getsource(stage), stage.__name__)


if __name__ == "__main__":
    unittest.main()
