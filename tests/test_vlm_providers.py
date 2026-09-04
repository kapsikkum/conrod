"""Four backends, one contract: send a prompt and some images, get back the
model's answer as a plain dict. Ollama is the only one that used to exist;
these tests are what makes it safe to add another provider later without
re-checking the other three by hand -- request shape (auth, schema, image
encoding) and response unwrapping are each pinned down per provider,
against a mocked httpx.Client so nothing here needs a real key or a real
network call.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from conrod import vlm, vlm_providers
from conrod.config import Settings

SCHEMA = {"type": "object", "properties": {"make": {"type": ["string", "null"]}},
         "required": ["make"]}


def _client(response_json, *, status=200):
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = response_json
    resp.raise_for_status.side_effect = (
        None if status < 400 else RuntimeError(f"status {status}"))
    client.post.return_value = resp
    return client, resp


class OllamaProvider(unittest.TestCase):
    def test_request_carries_the_model_prompt_images_and_schema(self) -> None:
        client, _ = _client({"response": json.dumps({"make": "Mini"})})
        settings = Settings(vlm_provider="ollama", vlm_model="qwen2.5vl:7b",
                            vlm_host="http://127.0.0.1:11434")
        vlm_providers.call(settings, prompt="describe it", images=["b64img"],
                           schema=SCHEMA, num_predict=500, client=client)
        url, kwargs = client.post.call_args[0][0], client.post.call_args[1]
        self.assertEqual(url, "http://127.0.0.1:11434/api/generate")
        body = kwargs["json"]
        self.assertEqual(body["model"], "qwen2.5vl:7b")
        self.assertEqual(body["prompt"], "describe it")
        self.assertEqual(body["images"], ["b64img"])
        self.assertEqual(body["format"], SCHEMA)

    def test_falls_back_to_thinking_when_response_is_empty(self) -> None:
        """qwen3-vl puts the answer in "thinking" and leaves "response"
        empty even with think disabled."""
        client, _ = _client({"response": "", "thinking": json.dumps({"make": "BMW"})})
        result = vlm_providers.call(Settings(vlm_provider="ollama"), prompt="p",
                                    images=[], schema=SCHEMA, num_predict=1,
                                    client=client)
        self.assertEqual(result, {"make": "BMW"})

    def test_result_is_the_parsed_json(self) -> None:
        client, _ = _client({"response": json.dumps({"make": "Subaru"})})
        result = vlm_providers.call(Settings(vlm_provider="ollama"), prompt="p",
                                    images=[], schema=SCHEMA, num_predict=1,
                                    client=client)
        self.assertEqual(result, {"make": "Subaru"})


class OpenAIProvider(unittest.TestCase):
    def test_the_key_goes_in_the_bearer_header(self) -> None:
        client, _ = _client({"choices": [{"message": {"content": "{}"}}]})
        settings = Settings(vlm_provider="openai", vlm_model="gpt-4o",
                            vlm_api_key="sk-test-123")
        vlm_providers.call(settings, prompt="p", images=["b64img"], schema=SCHEMA,
                           num_predict=500, client=client)
        headers = client.post.call_args[1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-test-123")

    def test_the_image_goes_in_as_a_data_uri(self) -> None:
        client, _ = _client({"choices": [{"message": {"content": "{}"}}]})
        vlm_providers.call(Settings(vlm_provider="openai", vlm_api_key="k"),
                           prompt="p", images=["b64img"], schema=SCHEMA,
                           num_predict=1, client=client)
        content = client.post.call_args[1]["json"]["messages"][0]["content"]
        image_block = next(c for c in content if c["type"] == "image_url")
        self.assertEqual(image_block["image_url"]["url"],
                         "data:image/jpeg;base64,b64img")

    def test_strict_mode_needs_additional_properties_false(self) -> None:
        """Required for strict structured outputs to actually be enforced --
        and it must not mutate the shared SCHEMA every other provider uses
        as-is."""
        client, _ = _client({"choices": [{"message": {"content": "{}"}}]})
        vlm_providers.call(Settings(vlm_provider="openai", vlm_api_key="k"),
                           prompt="p", images=[], schema=SCHEMA, num_predict=1,
                           client=client)
        sent_schema = client.post.call_args[1]["json"]["response_format"]["json_schema"]["schema"]
        self.assertFalse(sent_schema["additionalProperties"])
        self.assertNotIn("additionalProperties", SCHEMA)

    def test_result_is_parsed_from_message_content(self) -> None:
        client, _ = _client({"choices": [{"message": {
            "content": json.dumps({"make": "Ford"})}}]})
        result = vlm_providers.call(Settings(vlm_provider="openai", vlm_api_key="k"),
                                    prompt="p", images=[], schema=SCHEMA,
                                    num_predict=1, client=client)
        self.assertEqual(result, {"make": "Ford"})


class AnthropicProvider(unittest.TestCase):
    def test_the_key_goes_in_x_api_key_not_bearer(self) -> None:
        client, _ = _client({"content": [{"type": "tool_use", "input": {}}]})
        settings = Settings(vlm_provider="anthropic", vlm_model="claude-sonnet-5",
                            vlm_api_key="sk-ant-test")
        vlm_providers.call(settings, prompt="p", images=[], schema=SCHEMA,
                           num_predict=1, client=client)
        headers = client.post.call_args[1]["headers"]
        self.assertEqual(headers["x-api-key"], "sk-ant-test")
        self.assertNotIn("Authorization", headers)

    def test_the_tool_is_forced_so_the_reply_is_never_free_text(self) -> None:
        client, _ = _client({"content": [{"type": "tool_use", "input": {}}]})
        vlm_providers.call(Settings(vlm_provider="anthropic", vlm_api_key="k"),
                           prompt="p", images=[], schema=SCHEMA, num_predict=1,
                           client=client)
        body = client.post.call_args[1]["json"]
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "describe_vehicle"})
        self.assertEqual(body["tools"][0]["input_schema"], SCHEMA)

    def test_result_is_the_tool_calls_own_input(self) -> None:
        """Anthropic hands the tool call's input back already parsed --
        unlike the other three, there is no JSON string to decode here."""
        client, _ = _client({"content": [
            {"type": "text", "text": "thinking out loud"},
            {"type": "tool_use", "name": "describe_vehicle", "input": {"make": "Holden"}},
        ]})
        result = vlm_providers.call(Settings(vlm_provider="anthropic", vlm_api_key="k"),
                                    prompt="p", images=[], schema=SCHEMA,
                                    num_predict=1, client=client)
        self.assertEqual(result, {"make": "Holden"})

    def test_no_tool_call_in_the_reply_is_a_failure_not_an_empty_answer(self) -> None:
        client, _ = _client({"content": [{"type": "text", "text": "no thanks"}]})
        with self.assertRaises(ValueError):
            vlm_providers.call(Settings(vlm_provider="anthropic", vlm_api_key="k"),
                               prompt="p", images=[], schema=SCHEMA, num_predict=1,
                               client=client)


class GeminiProvider(unittest.TestCase):
    def test_the_key_is_a_query_parameter_not_a_header(self) -> None:
        client, _ = _client({"candidates": [{"content": {"parts": [
            {"text": "{}"}]}}]})
        settings = Settings(vlm_provider="gemini", vlm_model="gemini-2.0-flash",
                            vlm_api_key="AIzaTest")
        vlm_providers.call(settings, prompt="p", images=[], schema=SCHEMA,
                           num_predict=1, client=client)
        self.assertIn("gemini-2.0-flash", client.post.call_args[0][0])
        self.assertEqual(client.post.call_args[1]["params"], {"key": "AIzaTest"})

    def test_result_is_parsed_from_the_first_candidates_text(self) -> None:
        client, _ = _client({"candidates": [{"content": {"parts": [
            {"text": json.dumps({"make": "Toyota"})}]}}]})
        result = vlm_providers.call(Settings(vlm_provider="gemini", vlm_api_key="k"),
                                    prompt="p", images=[], schema=SCHEMA,
                                    num_predict=1, client=client)
        self.assertEqual(result, {"make": "Toyota"})


class Dispatch(unittest.TestCase):
    def test_an_unknown_provider_is_refused_rather_than_silently_using_ollama(self) -> None:
        with self.assertRaises(ValueError):
            vlm_providers.call(Settings(vlm_provider="not-a-real-provider"),
                               prompt="p", images=[], schema=SCHEMA, num_predict=1,
                               client=MagicMock())

    def test_no_provider_set_defaults_to_ollama(self) -> None:
        client, _ = _client({"response": json.dumps({"make": "Mazda"})})
        result = vlm_providers.call(Settings(vlm_provider=""), prompt="p", images=[],
                                    schema=SCHEMA, num_predict=1, client=client)
        self.assertEqual(result, {"make": "Mazda"})
        self.assertIn("/api/generate", client.post.call_args[0][0])


class DescribeDispatchesByProvider(unittest.TestCase):
    """vlm.describe() no longer knows there is more than one backend -- it
    builds the same prompt and schema regardless, and vlm_providers.call()
    is the only thing that reads settings.vlm_provider. Checked here without
    a real network call in either direction."""

    def _crop(self):
        from PIL import Image

        return Image.new("RGB", (32, 32), "grey")

    def test_describe_asks_vlm_providers_for_whichever_provider_is_set(self) -> None:
        with patch.object(vlm_providers, "call",
                          return_value={"make": "Toyota", "model": "86"}) as mocked:
            result = vlm.describe(self._crop(), Settings(vlm_provider="gemini",
                                                          vlm_api_key="k"))
        self.assertEqual(mocked.call_args[0][0].vlm_provider, "gemini")
        self.assertEqual(result.make, "Toyota")

    def test_a_provider_failure_is_an_empty_description_not_a_crash(self) -> None:
        with patch.object(vlm_providers, "call", side_effect=RuntimeError("boom")):
            result = vlm.describe(self._crop(), Settings(vlm_provider="openai",
                                                          vlm_api_key="k"))
        self.assertEqual(result, vlm.VehicleDescription())

    def test_check_available_asks_the_right_providers_own_check(self) -> None:
        # _AVAILABILITY_CHECKS captures the function object at module load,
        # so the dict entry is what has to be patched -- not the module
        # attribute, which check_available() never looks up again.
        fake_check = MagicMock()
        with patch.dict(vlm._AVAILABILITY_CHECKS, {"gemini": fake_check}):
            vlm.check_available(Settings(vlm_provider="gemini", vlm_api_key="k"))
        fake_check.assert_called_once()

    def test_an_unconfigured_cloud_provider_fails_before_any_network_call(self) -> None:
        for provider in ("openai", "anthropic", "gemini"):
            with self.assertRaises(vlm.VLMUnavailable):
                vlm.check_available(Settings(vlm_provider=provider, vlm_api_key=""))


if __name__ == "__main__":
    unittest.main()
