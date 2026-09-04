"""HTTP shape for each vision-model backend.

Everything that is *what to ask* -- the prompt, the JSON schema, how a
reply maps onto a VehicleDescription -- lives in vlm.py and is the same
regardless of which one of these actually answers. Everything here is
purely *how to ask that provider* -- the endpoint, the auth header, how an
image and a schema get shaped into that API's own request, and how to pull
the model's JSON back out of that API's own response envelope.

Every function here takes the same six arguments and returns the same
thing: the model's answer as a plain dict, already parsed from whatever
shape that provider wraps its JSON in. Any failure -- network, auth, a
malformed reply -- is left to raise; the caller in vlm.py already treats
"could not describe this vehicle" as one case, not four provider-specific
ones.
"""

from __future__ import annotations

import email.utils
import json
import random
import threading
import time

import httpx

from .config import Settings

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


# ── rate limits ──────────────────────────────────────────────────
# A shoot is thousands of crops and every cloud provider meters them, so
# 429 is a normal part of a scan rather than an error in it. Left alone it
# was the worst possible outcome: the caller in vlm.py turns any exception
# into an empty description, so a rate-limited scan quietly produced frames
# with nothing read off them and no sign of why.
#
# The wait is shared. The analysis pool runs several workers at once, and
# without a common gate each one discovers the limit separately and keeps
# hammering while the others back off -- so one 429 holds all of them.

RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 529}
MAX_WAIT = 60.0

_gate = threading.Lock()
_not_before = 0.0


def _hold_off(seconds: float) -> None:
    """Ask every worker to wait, not just the one that was refused."""
    global _not_before
    with _gate:
        _not_before = max(_not_before, time.monotonic() + seconds)


def _wait_turn() -> None:
    with _gate:
        delay = _not_before - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """How long the provider asked for, or a backed-off guess."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(MAX_WAIT, max(0.0, float(header)))
        except ValueError:
            try:                                    # an HTTP date
                when = email.utils.parsedate_to_datetime(header)
                return min(MAX_WAIT, max(0.0, when.timestamp() - time.time()))
            except (TypeError, ValueError):
                pass
    # Jittered, so several workers refused at once do not all come back in
    # the same instant and trip the limit again together.
    return min(MAX_WAIT, (2 ** attempt) + random.uniform(0, 1))


def _send(send, settings: Settings, what: str) -> httpx.Response:
    """Make the request, waiting out rate limits rather than failing on one."""
    attempts = max(1, getattr(settings, "vlm_max_retries", 4))
    last: Exception | None = None
    for attempt in range(attempts):
        _wait_turn()
        try:
            response = send()
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            last = exc
            if exc.response.status_code not in RETRY_STATUSES:
                raise
            if attempt == attempts - 1:
                break
            wait = _retry_after(exc.response, attempt)
            _hold_off(wait)
            _note(f"{what} is rate limiting; waiting {wait:.0f}s "
                  f"(attempt {attempt + 1} of {attempts})")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == attempts - 1:
                break
            wait = _retry_after(httpx.Response(503), attempt)
            _hold_off(wait)
            _note(f"{what} did not answer ({exc}); waiting {wait:.0f}s")
    raise last if last else RuntimeError(f"{what} could not be reached")


def _note(message: str) -> None:
    """Rate limits are slow, not silent -- a scan that has quietly stopped
    for four minutes should say which of the two it is."""
    try:
        from .config import LOG_PATH

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as fh:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fh.write(stamp + "  " + message + chr(10))
    except Exception:
        pass


def call(settings: Settings, *, prompt: str, images: list[str], schema: dict,
        num_predict: int, client: httpx.Client) -> dict:
    """Ask whichever provider is configured. Returns the parsed JSON reply."""
    provider = (settings.vlm_provider or "ollama").lower()
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise ValueError(f"unknown vision provider '{provider}'")
    return fn(settings, prompt, images, schema, num_predict, client)


def _ollama(settings: Settings, prompt: str, images: list[str], schema: dict,
           num_predict: int, client: httpx.Client) -> dict:
    payload = {
        "model": settings.vlm_model,
        "prompt": prompt,
        "images": images,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    resp = client.post(f"{settings.vlm_host}/api/generate", json=payload,
                       timeout=settings.vlm_timeout)
    resp.raise_for_status()
    data = resp.json()
    # Reasoning models (qwen3-vl, for one) put the answer in "thinking" and
    # leave "response" empty even with think disabled.
    body = (data.get("response") or "").strip() or (data.get("thinking") or "")
    return json.loads(body)


def _openai(settings: Settings, prompt: str, images: list[str], schema: dict,
           num_predict: int, client: httpx.Client) -> dict:
    content = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image}"}})
    # Strict structured outputs require every property to be listed as
    # required and additionalProperties set explicitly -- both already true
    # of SCHEMA except the second, so it is added here rather than on the
    # shared schema every other provider also uses.
    strict_schema = {**schema, "additionalProperties": False}
    payload = {
        "model": settings.vlm_model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "vehicle", "schema": strict_schema, "strict": True}},
        "max_tokens": num_predict,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {settings.vlm_api_key}"}
    resp = _send(lambda: client.post(OPENAI_URL, json=payload, headers=headers,
                                     timeout=settings.vlm_timeout),
                 settings, "OpenAI")
    data = resp.json()
    return json.loads(data["choices"][0]["message"]["content"])


# Claude Code's tokens are prefixed distinctly enough to recognise, which
# is what makes an unset key type safe to infer rather than assume.
CLAUDE_CODE_PREFIXES = ("sk-ant-oat01-", "sk-ant-ort01-")


def anthropic_key_kind(settings: Settings) -> str:
    """Which kind of credential this is: "api-key" or "claude-code".

    The Settings choice wins where one has been made. On "auto" -- and for
    settings written before the field existed, where it is null -- the
    token's own prefix decides, because a fixed default could not be told
    apart from a deliberate choice, and quietly sent Claude Code tokens on
    the API-key header for a 401 reading "API key is invalid".
    """
    chosen = (getattr(settings, "anthropic_key_kind", None) or "auto").strip().lower()
    if chosen in ("api-key", "claude-code"):
        return chosen
    key = (settings.vlm_api_key or "").strip()
    return "claude-code" if key.startswith(CLAUDE_CODE_PREFIXES) else "api-key"


def anthropic_auth(settings: Settings) -> dict:
    """Whichever single header the chosen kind of credential belongs on.

    A console API key authenticates on x-api-key; a Claude Code OAuth token
    on Authorization: Bearer. Sending both would put the credential on the
    wire twice, so exactly one goes out.
    """
    if anthropic_key_kind(settings) == "claude-code":
        return {"Authorization": f"Bearer {settings.vlm_api_key}"}
    return {"x-api-key": settings.vlm_api_key}


def _anthropic(settings: Settings, prompt: str, images: list[str], schema: dict,
              num_predict: int, client: httpx.Client) -> dict:
    content = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": image}})
    # Claude has no grammar-constrained JSON mode like Ollama's. A single
    # forced tool call gets the same guarantee: the tool's input_schema is
    # SCHEMA, tool_choice forces that exact tool, and the call's "input" is
    # the parsed answer -- no "output raw JSON" prompt instruction needed.
    # No "temperature": current models reject it outright -- "`temperature`
    # is deprecated for this model", a 400 that swallowed every
    # identification as an empty answer. The forced tool call is what makes
    # the reply deterministic in shape anyway.
    payload = {
        "model": settings.vlm_model,
        "max_tokens": num_predict,
        "tools": [{"name": "describe_vehicle",
                  "description": "Record the extracted vehicle fields.",
                  "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": "describe_vehicle"},
        "messages": [{"role": "user", "content": content}],
    }
    headers = {"anthropic-version": ANTHROPIC_VERSION,
              **anthropic_auth(settings)}
    resp = _send(lambda: client.post(ANTHROPIC_URL, json=payload, headers=headers,
                                     timeout=settings.vlm_timeout),
                 settings, "Anthropic")
    data = resp.json()
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            return block.get("input") or {}
    raise ValueError("Claude did not return the forced tool call")


def _gemini(settings: Settings, prompt: str, images: list[str], schema: dict,
           num_predict: int, client: httpx.Client) -> dict:
    parts = [{"text": prompt}]
    for image in images:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        # Gemini's schema dialect is not the same one Ollama/OpenAI use --
        # nullable fields are "nullable": true beside a single type, not a
        # ["string", "null"] union -- so SCHEMA is not reusable as-is here.
        # responseMimeType alone still forces valid JSON, and the prompt
        # already spells out the exact structure wanted.
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": num_predict,
                             "responseMimeType": "application/json"},
    }
    url = GEMINI_URL.format(model=settings.vlm_model)
    resp = _send(lambda: client.post(url, json=payload,
                                     params={"key": settings.vlm_api_key},
                                     timeout=settings.vlm_timeout),
                 settings, "Gemini")
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


# How each one is spelled when shown to someone. "openai".capitalize() is
# "Openai", which looks like a typo of a company's own name.
DISPLAY_NAMES = {
    "ollama": "Ollama", "openai": "OpenAI",
    "anthropic": "Anthropic", "gemini": "Gemini",
}

_PROVIDERS = {
    "ollama": _ollama,
    "openai": _openai,
    "anthropic": _anthropic,
    "gemini": _gemini,
}
