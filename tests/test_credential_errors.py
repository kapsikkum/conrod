"""Anthropic takes two kinds of credential on two different headers: a
console API key on x-api-key, a Claude Code OAuth token on Authorization:
Bearer. They cannot be told apart from the string reliably enough to
guess, and sending both would put the credential on the wire twice -- so
Settings asks which it is, and only that header goes out.

The failure this replaces: a token sent on the wrong header comes back as
a bare 401 from api.anthropic.com, with a link to MDN's page about the
status code, which says nothing about the actual problem.
"""

from __future__ import annotations

import unittest

from conrod import vlm, vlm_providers
from conrod.config import Settings


class OneHeaderNotBoth(unittest.TestCase):
    def test_an_api_key_goes_on_x_api_key(self) -> None:
        auth = vlm_providers.anthropic_auth(
            Settings(anthropic_key_kind="api-key", vlm_api_key="sk-ant-api03-x"))
        self.assertEqual(auth, {"x-api-key": "sk-ant-api03-x"})

    def test_a_claude_code_token_goes_on_the_bearer_header(self) -> None:
        auth = vlm_providers.anthropic_auth(
            Settings(anthropic_key_kind="claude-code", vlm_api_key="sk-ant-oat01-x"))
        self.assertEqual(auth, {"Authorization": "Bearer sk-ant-oat01-x"})

    def test_the_credential_is_never_sent_twice(self) -> None:
        for kind in ("api-key", "claude-code"):
            auth = vlm_providers.anthropic_auth(
                Settings(anthropic_key_kind=kind, vlm_api_key="secret"))
            self.assertEqual(len(auth), 1, auth)

    def test_the_default_reads_the_kind_off_the_key(self) -> None:
        """A fixed default could not be told apart from someone choosing
        that value, so a Claude Code token went out on the API-key header
        and came back "API key is invalid"."""
        self.assertEqual(Settings().anthropic_key_kind, "auto")
        self.assertIn("x-api-key", vlm_providers.anthropic_auth(
            Settings(vlm_api_key="sk-ant-api03-x")))
        self.assertIn("Authorization", vlm_providers.anthropic_auth(
            Settings(vlm_api_key="sk-ant-oat01-x")))

    def test_settings_written_before_the_field_existed_still_work(self) -> None:
        """They have it as null, which must behave as auto rather than as a
        choice of api-key."""
        stale = Settings(vlm_api_key="sk-ant-oat01-x")
        stale.anthropic_key_kind = None
        self.assertIn("Authorization", vlm_providers.anthropic_auth(stale))


class WhenTheSettingAndTheKeyDisagree(unittest.TestCase):
    """A good credential on the wrong header is refused exactly like a bad
    one, so the mismatch is worth catching before anything is sent."""

    def _why(self, kind: str, key: str) -> str:
        with self.assertRaises(vlm.VLMUnavailable) as caught:
            vlm.check_available(Settings(vlm_provider="anthropic",
                                         vlm_model="claude-sonnet-5",
                                         anthropic_key_kind=kind,
                                         vlm_api_key=key))
        return str(caught.exception)

    def test_a_claude_code_token_filed_as_an_api_key(self) -> None:
        why = self._why("api-key", "sk-ant-oat01-example")
        self.assertIn("Claude Code", why)
        self.assertIn("claude-code", why)

    def test_a_refresh_token_is_caught_the_same_way(self) -> None:
        self.assertIn("Claude Code", self._why("api-key", "sk-ant-ort01-example"))

    def test_an_api_key_filed_as_a_claude_code_token(self) -> None:
        self.assertIn("api-key", self._why("claude-code", "sk-ant-api03-example"))

    def test_it_is_settled_before_anything_is_sent(self) -> None:
        import httpx

        def explode(*a, **kw):
            raise AssertionError("no request should be made for a known mismatch")

        real, httpx.get = httpx.get, explode
        try:
            self._why("api-key", "sk-ant-oat01-example")
        finally:
            httpx.get = real

    def test_a_missing_key_still_says_so_plainly(self) -> None:
        self.assertIn("No Anthropic API key set", self._why("api-key", ""))


class TheReasonAKeyWasRefused(unittest.TestCase):
    def _classify(self, status):
        import httpx

        request = httpx.Request("GET", "https://example.test")
        response = httpx.Response(status, request=request)
        exc = httpx.HTTPStatusError("boom", request=request, response=response)
        return str(vlm._rejected("Anthropic", exc, "console.anthropic.com",
                                 "claude-sonnet-5"))

    def test_a_401_points_at_the_key_and_the_billing(self) -> None:
        why = self._classify(401)
        self.assertIn("rejected the key", why)
        self.assertIn("credit", why)

    def test_a_404_points_at_the_model_field(self) -> None:
        why = self._classify(404)
        self.assertIn("claude-sonnet-5", why)
        self.assertIn("Model field", why)

    def test_anything_else_reads_as_unreachable(self) -> None:
        self.assertIn("Cannot reach", self._classify(500))


if __name__ == "__main__":
    unittest.main()
