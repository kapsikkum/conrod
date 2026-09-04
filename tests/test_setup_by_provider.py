"""The setup screen offered to install "claude-sonnet-5", about 6 GB to
download, with an Install button that would have asked Ollama to pull it.

That check was written when Ollama was the only option and the model was
always something local and large. Once the provider could be OpenAI,
Anthropic or Gemini it was nonsense: there is nothing to fetch, and the
only question worth asking is whether a key has been set.
"""

from __future__ import annotations

import unittest

from conrod import setup_check
from conrod.config import Settings


def vlm_checks(**kw):
    env = setup_check.inspect(Settings(**kw))
    return {c.key: c for c in env.checks}


class ACloudProviderHasNothingToInstall(unittest.TestCase):
    def test_no_download_is_offered(self) -> None:
        checks = vlm_checks(vlm_provider="anthropic", vlm_model="claude-sonnet-5")
        self.assertIsNone(checks["vlm"].fix)
        self.assertNotIn("GB", checks["vlm"].detail)
        self.assertNotIn("install", checks["vlm"].detail.lower())

    def test_ollama_is_not_even_looked_at(self) -> None:
        """Whether a local server is running has nothing to do with whether
        Gemini will answer."""
        self.assertNotIn("ollama", vlm_checks(vlm_provider="gemini", vlm_api_key="k"))

    def test_a_missing_key_is_what_it_reports_instead(self) -> None:
        check = vlm_checks(vlm_provider="openai", vlm_model="gpt-4o")["vlm"]
        self.assertFalse(check.ok)
        self.assertIn("API key", check.detail)

    def test_a_key_that_is_set_reads_as_ready(self) -> None:
        check = vlm_checks(vlm_provider="openai", vlm_model="gpt-4o",
                           vlm_api_key="sk-test")["vlm"]
        self.assertTrue(check.ok)
        self.assertIn("gpt-4o", check.detail)
        self.assertIn("OpenAI", check.detail)

    def test_it_is_never_a_reason_to_block_the_app(self) -> None:
        """Identifying is the good part, not a requirement -- without it the
        app still reads plates, numbers and text."""
        for provider in ("openai", "anthropic", "gemini"):
            self.assertFalse(vlm_checks(vlm_provider=provider)["vlm"].required)

    def test_turning_the_vision_model_off_still_short_circuits(self) -> None:
        check = vlm_checks(use_vlm=False, vlm_provider="anthropic")["vlm"]
        self.assertTrue(check.ok)
        self.assertIn("disabled", check.detail)


if __name__ == "__main__":
    unittest.main()
