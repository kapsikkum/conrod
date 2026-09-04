"""Vehicle understanding with a vision-language model.

Local Ollama is the default -- it needs nothing else installed, sends
nothing off the machine, and ships its own CUDA runtime, so it uses the
discrete GPU even when the installed PyTorch is a CPU build. OpenAI,
Anthropic and Gemini are also available for whoever would rather send crops
to a cloud model; settings.vlm_provider picks which, and the request/response
shape for each one lives in vlm_providers.py. Everything below -- the
prompts, the schema, what a reply maps onto -- is the same regardless.

The model answers the semantic questions — what car is this, what colour,
whose team, what does the livery say. It is deliberately *not* asked to read
the plate: measured on a 6960x4640 frame, Ollama returns null for the plate
at every input resolution, because the plate is only a few dozen pixels wide
once the crop is scaled to fit. Plates go through plates.py instead.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from PIL import Image

from . import marques, vlm_providers
from .config import Settings

CAR_PROMPT = """Context: You are analyzing a professional automotive and motorsport photograph of a single vehicle. 

Task: Extract data fields strictly from the vehicle into the JSON template below. Do not infer, guess, or extrapolate details that are not visibly present in the image.

Rules:
1. Report only what is clearly legible. If a field cannot be seen, read, or identified with certainty, use a literal JSON null value.
2. Look closely at the windows, fenders, and roof edges for small driver, team, or entrant names, which are frequently positioned directly next to a national flag decal.
3. Never guess or extract a registration/license plate; ignore it entirely.
4. Do not report text from trackside signage, banners, barriers, spectators, or the background. Only extract text physically on the vehicle.
5. Output strictly raw JSON matching the template exactly. Do not include markdown formatting, backticks, or conversational text.

Return only this JSON structure:
{
  "make": "The manufacturer name string",
  "model": "The model and variant string if badged or clearly identifiable",
  "colour": "The base exterior colour as an everyday colour word, e.g. blue, silver, black, white -- not a manufacturer paint name",
  "body_type": "One of: hatchback, sedan, wagon, ute, coupe, convertible, SUV, van, truck",
  "race_number": "Digits only from the competition number on the door, bonnet, or roundel. Do not include letters.",
  "team": "The race team, entrant, or driver name string if physically shown on the vehicle (often found next to flag decals)",
  "sponsors": ["Array of corporate sponsor brand names visible on the livery"],
  "livery_text": ["Array of any other readable words, URLs, or slogans on the vehicle"],
  "is_competition": true/false,
  "confidence": "Your overall certainty score for the extraction as a float from 0.0 to 1.0"
}"""

BIKE_PROMPT = """Context: You are analyzing a professional motorsport photograph of a single motorcycle and its rider.

Task: Extract data fields strictly from the motorcycle and the rider into the JSON template below. Do not infer, guess, or extrapolate details that are not visibly present in the image.

Rules:
1. Report only what is clearly legible. If a field cannot be seen, read, or identified with certainty, use a literal JSON null value.
2. Look at both the motorcycle fairings/bodywork AND the rider's racing leathers (including the back, chest, sleeves, and aero hump) to find race numbers, teams, and sponsors.
3. Do not report text from trackside signage, banners, barriers, spectators, or the background. Only extract text physically on the motorcycle or rider.
4. Output strictly raw JSON matching the template exactly. Do not include markdown formatting, backticks, or conversational text.

Return only this JSON structure:
{
  "make": "The manufacturer name string",
  "model": "The model name string if badged or clearly identifiable",
  "colour": "The dominant color string of the motorcycle bodywork or fairing",
  "body_type": "motorcycle",
  "race_number": "Digits only from the competition number on the front fairing, tail, side boards, or rider's leathers. Do not include letters.",
  "team": "The race team, entrant, or rider name string if physically shown on the bike or leathers",
  "sponsors": ["Array of corporate sponsor brand names visible on the bike fairings or rider's leathers"],
  "livery_text": ["Array of any other readable words, URLs, or slogans on the bike or rider"],
  "is_competition": true/false,
  "confidence": "Your overall certainty score for the extraction as a float from 0.0 to 1.0"
}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "make": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "colour": {"type": ["string", "null"]},
        "body_type": {"type": ["string", "null"]},
        "race_number": {"type": ["string", "null"]},
        "team": {"type": ["string", "null"]},
        "sponsors": {"type": "array", "items": {"type": "string"}},
        "livery_text": {"type": "array", "items": {"type": "string"}},
        "is_competition": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["make", "model", "colour", "body_type", "race_number",
                 "team", "sponsors", "livery_text", "confidence"],
}


@dataclass
class VehicleDescription:
    make: str | None = None
    model: str | None = None
    colour: str | None = None
    body_type: str | None = None
    race_number: str | None = None
    team: str | None = None
    sponsors: list[str] = field(default_factory=list)
    livery_text: list[str] = field(default_factory=list)
    is_competition: bool = False
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "make": self.make, "model": self.model, "colour": self.colour,
            "body_type": self.body_type, "race_number": self.race_number,
            "team": self.team, "sponsors": self.sponsors,
            "livery_text": self.livery_text,
            "is_competition": self.is_competition, "confidence": self.confidence,
        }

    @property
    def title(self) -> str:
        parts = [self.colour, self.make, self.model]
        return " ".join(p for p in parts if p)


def provider_message(response) -> str:
    """What the provider actually said, whichever envelope it said it in.

    Every one of them reports the reason in a different place, and reading
    only some of them is worse than reading none: one identify run logged
    "describe failed: HTTP 404" three thousand times because the reason sat
    in a field this did not look at. The three shapes:

        OpenAI/Anthropic  {"error": {"type": ..., "message": ...}}
        Gemini            {"error": {"status": ..., "message": ...}}
        Ollama            {"error": "model 'x' not found"}
    """
    try:
        body = response.json()
    except Exception:
        text = (getattr(response, "text", "") or "").strip()
        return text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, str):
        return error[:200]
    if isinstance(error, dict):
        said = error.get("message") or error.get("type") or error.get("status")
        if said:
            return str(said)[:200]
    return ""


def _brief(exc: Exception) -> str:
    """One line naming the cause, in the provider's own words.

    An httpx error stringifies to a URL and a link to MDN, which says
    nothing about which of the possible faults this was -- and a bare status
    code is barely better. A 404 from Anthropic means one of "no such
    model", "that credential cannot call this endpoint" and "the URL is
    wrong", and only the body distinguishes them.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        said = provider_message(response)
        return f"HTTP {response.status_code} {said}".strip()
    return f"{type(exc).__name__}: {exc}"


def _failed(what: str, settings: Settings, exc: Exception) -> None:
    """Record one failed call, and stop the run if they are all failing.

    The line says which provider and which model, because "describe failed:
    HTTP 404" read the same whichever of the four was configured and
    whatever was wrong with it.
    """
    who = vlm_providers.DISPLAY_NAMES.get(
        (settings.vlm_provider or "ollama").lower(), settings.vlm_provider)
    said = _brief(exc)
    vlm_providers._note(f"{who} ({settings.vlm_model}) {what} failed: {said}")

    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status not in vlm_providers.FATAL_STATUSES:
        vlm_providers.clear_fatal()
        return
    run = vlm_providers.note_fatal(said)
    if run < vlm_providers.GIVE_UP_AFTER:
        return
    raise vlm_providers.Misconfigured(
        f"{who} refused the last {run} vehicles the same way, so it will "
        f"refuse the rest: {said}. Nothing was identified. Check the model "
        f"name and API key in Settings, then run Identify again."
    )


class VLMUnavailable(RuntimeError):
    pass


def check_available(settings: Settings) -> None:
    """Fail early and clearly rather than once per crop, mid-run."""
    provider = (settings.vlm_provider or "ollama").lower()
    check = _AVAILABILITY_CHECKS.get(provider)
    if check is None:
        raise VLMUnavailable(f"Unknown vision provider '{provider}'.")
    check(settings)


def _check_ollama(settings: Settings) -> None:
    try:
        resp = httpx.get(f"{settings.vlm_host}/api/tags", timeout=5.0)
        resp.raise_for_status()
    except Exception as exc:
        raise VLMUnavailable(
            f"Cannot reach Ollama at {settings.vlm_host}. Is it running?"
        ) from exc

    names = {m.get("name", "") for m in resp.json().get("models", [])}
    if settings.vlm_model not in names and f"{settings.vlm_model}:latest" not in names:
        raise VLMUnavailable(
            f"Ollama has no model '{settings.vlm_model}'. "
            f"Pull it with:  ollama pull {settings.vlm_model}"
        )


# What each kind of Anthropic credential looks like. Used to catch the
# setting and the key disagreeing -- which otherwise arrives as a bare 401
# from somebody else's server, saying nothing about the actual problem.
_CLAUDE_CODE_PREFIXES = ("sk-ant-oat01-", "sk-ant-ort01-")
_API_KEY_PREFIXES = ("sk-ant-api",)


def _check_key(settings: Settings, name: str) -> None:
    """Every cloud provider needs at least this much before a real call is
    worth trying -- and a missing key otherwise surfaces as an opaque 401
    on the first crop of the scan rather than up front."""
    if not (settings.vlm_api_key or "").strip():
        raise VLMUnavailable(f"No {name} API key set. Add one in Settings.")


def _check_anthropic_kind(settings: Settings) -> None:
    """The key and the "key type" setting must agree, because they decide
    which header it is sent on. Sent on the wrong one, a perfectly good
    credential comes back 401 and looks like a bad key."""
    key = (settings.vlm_api_key or "").strip()
    # Only a choice someone actually made can disagree with the key; an
    # unset one is inferred from the key itself and never will.
    kind = (getattr(settings, "anthropic_key_kind", None) or "").strip().lower()
    if kind == "api-key" and key.startswith(_CLAUDE_CODE_PREFIXES):
        raise VLMUnavailable(
            "That looks like a Claude Code token, but the key type is set to "
            "\"api-key\". Set it to \"claude-code\", or paste an API key "
            "from console.anthropic.com instead.")
    if kind == "claude-code" and key.startswith(_API_KEY_PREFIXES):
        raise VLMUnavailable(
            "That looks like a console API key, but the key type is set to "
            "\"claude-code\". Set it to \"api-key\".")


def _rejected(name: str, exc: Exception, where: str,
             model: str | None = None) -> VLMUnavailable:
    """Say which kind of failure it was. A refused key, an unknown model and
    an unreachable host all arrive as an exception from httpx, and reporting
    the raw one leaves someone reading a stack of URLs to work out that the
    answer is "that is the wrong sort of credential"."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    said = provider_message(response) if response is not None else ""
    if status in (401, 403):
        return VLMUnavailable(
            f"{name} rejected the key: {said or 'not authorised'}. Check it "
            f"is a current API key from {where}, and that the account it "
            f"belongs to has credit.")
    if status == 404:
        # Two different faults share this code, and the provider's own
        # sentence is what separates them: a model name that does not exist,
        # and a credential that exists but is not allowed to call this
        # endpoint. Guessing "no such model" for both sent one photographer
        # looking for a typo in a model name that was spelled correctly.
        return VLMUnavailable(
            f"{name} would not answer for model '{model}': {said or 'not found'}. "
            f"Check the Model field in Settings, and that the key is one "
            f"from {where} rather than another kind of token.")
    # Everything else -- a 500, a timeout, a refused connection -- is the
    # provider being unwell rather than the configuration being wrong, and
    # is worth trying again rather than worth changing a setting over.
    return VLMUnavailable(f"Cannot reach {name}: {said or exc}")


def _trial_image() -> str:
    """A postage stamp to send with the trial call.

    The trial has to carry a picture, because half of what can be wrong is
    wrong only for requests that have one -- a text-only model, an endpoint
    that takes no image block, a provider that meters vision separately.
    """
    return _encode(Image.new("RGB", (32, 32), (128, 128, 128)), 32)


def _trial_call(settings: Settings, name: str, where: str) -> None:
    """Ask the provider the exact question the scan will, once.

    The checks here used to fetch the provider's model list, which proves
    only that the key can read a list. A Claude Code OAuth token can do
    that and cannot call /v1/messages at all: the check passed, the run
    started, and every crop came back 404. The one thing worth asking is
    the thing the run depends on, so this sends a real request down the
    real code path -- same endpoint, same auth header, same model name,
    same schema dialect, same response envelope -- and reads it back.

    Costs one call of a few dozen tokens at the start of a run, against
    thousands in it.
    """
    try:
        with httpx.Client(timeout=min(30.0, settings.vlm_timeout)) as client:
            vlm_providers.call(settings, prompt="Reply with an empty JSON object.",
                               images=[_trial_image()], schema=SCHEMA,
                               num_predict=64, client=client)
    except httpx.HTTPStatusError as exc:
        raise _rejected(name, exc, where, settings.vlm_model) from exc
    except httpx.RequestError as exc:
        raise VLMUnavailable(f"Cannot reach {name}: {exc}") from exc
    except Exception:
        # It answered. Whatever it found to say about a blank grey tile is
        # not evidence about the album, and refusing to start the run over
        # it would be the check inventing its own failure.
        return


def _check_openai(settings: Settings) -> None:
    _check_key(settings, "OpenAI")
    _trial_call(settings, "OpenAI", "platform.openai.com")


def _check_anthropic(settings: Settings) -> None:
    _check_key(settings, "Anthropic")
    _check_anthropic_kind(settings)
    _trial_call(settings, "Anthropic", "console.anthropic.com")


def _check_gemini(settings: Settings) -> None:
    _check_key(settings, "Gemini")
    _trial_call(settings, "Gemini", "aistudio.google.com")


_AVAILABILITY_CHECKS = {
    "ollama": _check_ollama,
    "openai": _check_openai,
    "anthropic": _check_anthropic,
    "gemini": _check_gemini,
}


def describe(image: Image.Image | Path, settings: Settings, *, is_bike: bool = False,
             client: httpx.Client | None = None) -> VehicleDescription:
    """Ask the model what this vehicle is."""
    if isinstance(image, Path):
        image = Image.open(image)

    payload_image = _encode(image, settings.vlm_input_edge)
    prompt = BIKE_PROMPT if is_bike else CAR_PROMPT

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.vlm_timeout)
    try:
        parsed = vlm_providers.call(settings, prompt=prompt, images=[payload_image],
                                    schema=SCHEMA, num_predict=500, client=client)
    except Exception as exc:
        # An empty description and a rate-limited one look identical on the
        # card -- a vehicle nobody could name. Saying which is the
        # difference between "the model could not read this car" and "the
        # whole scan is being throttled and none of it will be read".
        _failed("describe", settings, exc)
        return VehicleDescription()
    finally:
        if owns_client:
            client.close()

    make = _text(parsed.get("make"))
    model = _text(parsed.get("model"))
    # The nameplate is read more reliably than the badge; where the two
    # contradict each other and the nameplate belongs to exactly one marque,
    # the nameplate wins. See marques.py.
    make = marques.correct_make(make, model)

    return VehicleDescription(
        make=make,
        model=model,
        colour=_text(parsed.get("colour")),
        body_type=_text(parsed.get("body_type")),
        race_number=_digits(parsed.get("race_number"), settings),
        team=_text(parsed.get("team")),
        sponsors=_text_list(parsed.get("sponsors")),
        livery_text=_text_list(parsed.get("livery_text")),
        is_competition=bool(parsed.get("is_competition") or False),
        confidence=_number(parsed.get("confidence")),
    )


BURST_PROMPT = """These are {count} photographs of the SAME vehicle, taken
seconds apart in one burst by a motorsport photographer. Different angles,
different parts of the car legible in each.

Earlier attempts to identify it one photograph at a time disagreed with each
other. Use all of them together: a badge unreadable in one frame is often
plain in the next, and a shape ambiguous head-on is obvious in profile.

Answer for the vehicle, not for any one photograph. If the frames still do
not show enough to name the model, give the make alone and leave the model
null -- that is a useful answer. A confident guess is not.
"""


def identify_burst(images: "list[Image.Image | Path]", settings: Settings, *,
                   client: httpx.Client | None = None) -> VehicleDescription:
    """Ask about several views of one vehicle in a single call.

    This is the question the per-crop reader cannot be asked. It sees one
    frame, and one frame of a car going past at speed is often a three-quarter
    view with the badge blurred and the nameplate off the edge -- so it
    guesses, and guesses differently each time.

    Which frames get sent matters as much as sending several. They are chosen
    by measured subject sharpness upstream, so the model looks at the frames
    where the car is actually resolved rather than the ones where a panning
    blur has smeared the badge into the paint.
    """
    if not images:
        return VehicleDescription()

    encoded = []
    for item in images:
        try:
            image = Image.open(item) if isinstance(item, Path) else item
            encoded.append(_encode(image, settings.vlm_input_edge))
        except Exception:
            continue
    if not encoded:
        return VehicleDescription()

    prompt = BURST_PROMPT.format(count=len(encoded))

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.vlm_timeout)
    try:
        parsed = vlm_providers.call(settings, prompt=prompt, images=encoded,
                                    schema=SCHEMA, num_predict=400, client=client)
    except Exception as exc:
        _failed("burst read", settings, exc)
        return VehicleDescription()
    finally:
        if owns_client:
            client.close()

    make = marques.correct_make(_text(parsed.get("make")),
                               _text(parsed.get("model")))
    return VehicleDescription(
        make=make,
        model=_text(parsed.get("model")),
        colour=_text(parsed.get("colour")),
        body_type=_text(parsed.get("body_type")),
        team=_text(parsed.get("team")),
        sponsors=_text_list(parsed.get("sponsors")),
        livery_text=_text_list(parsed.get("livery_text")),
        is_competition=bool(parsed.get("is_competition") or False),
        confidence=_number(parsed.get("confidence")),
    )


def _encode(image: Image.Image, long_edge: int) -> str:
    """Downscale and JPEG-encode a crop for the model."""
    img = image.convert("RGB")
    if max(img.size) > long_edge:
        scale = long_edge / max(img.size)
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=92)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# Even with a JSON schema the model sometimes emits the four characters "null"
# as a string rather than a JSON null, so every field is filtered through this.
_NULLISH = {"", "null", "none", "n/a", "na", "unknown", "not visible",
            "not legible", "unreadable", "-"}


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _NULLISH else text


def _text_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        text = _text(item)
        if text and text.upper() not in seen:
            seen.add(text.upper())
            out.append(text)
    return out


def _digits(value, settings: Settings) -> str | None:
    text = _text(value)
    if not text:
        return None
    token = "".join(ch for ch in text if ch.isdigit())
    if not token:
        return None
    if not (settings.number_min_len <= len(token) <= settings.number_max_len):
        return None
    return token


def _number(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
