"""Vehicle understanding with a local vision-language model via Ollama.

This is the part that replaces ConrodAI's metered cloud call. Ollama ships
its own CUDA runtime, so this uses the discrete GPU even when the installed
PyTorch is a CPU build.

It answers the semantic questions — what car is this, what colour, whose team,
what does the livery say. It is deliberately *not* asked to read the plate:
measured on a 6960x4640 frame, the model returns null for the plate at every
input resolution, because the plate is only a few dozen pixels wide once the
crop is scaled to fit. Plates go through plates.py instead.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from PIL import Image

from . import marques
from .config import Settings

CAR_PROMPT = """This is a photograph of a single vehicle, taken by a motorsport
and car photographer.

Describe only what you can actually see:

- make: the manufacturer, e.g. Ford, Nissan, Holden, Subaru, BMW.
- model: model and variant if badged, e.g. "Focus RS", "WRX STI", "Mini Cooper S".
- colour: everyday colour name, e.g. blue, silver, black, white.
- body_type: hatchback, sedan, wagon, ute, coupe, convertible, SUV, van, truck.
- race_number: the competition number on the door, bonnet or roundel, if this is
  a competition car. Digits only. This is NOT the registration plate.
- team: the race team or entrant name, if shown on the vehicle.
- sponsors: brand names on the livery, as a list.
- livery_text: any other readable words or slogans on the vehicle, as a list.
- is_competition: true if this is set up for racing or track work (numbers,
  roundels, tow hooks, racing livery, competition tyres), false for a road car.

Rules:
- Report only what is legible. Use null for anything you cannot actually read.
- Never guess a registration plate; ignore it entirely.
- Do not report text from trackside signage, banners, barriers or the
  background. Only text physically on the vehicle.
- confidence is your overall certainty from 0.0 to 1.0."""

BIKE_PROMPT = """This is a photograph of a single motorcycle, taken by a
motorsport photographer.

Describe only what you can actually see:

- make: the manufacturer, e.g. Yamaha, Honda, Ducati, Kawasaki, Suzuki.
- model: the model if badged, e.g. "YZF-R1", "Panigale V4".
- colour: the dominant colour of the bodywork or fairing.
- body_type: always "motorcycle".
- race_number: the competition number on the fairing, number board or the
  rider's leathers. Digits only.
- team: the race team or entrant name, if shown.
- sponsors: brand names on the fairing or leathers, as a list.
- livery_text: any other readable words on the bike or rider, as a list.
- is_competition: true if this is a race or track bike.

Rules:
- Report only what is legible. Use null for anything you cannot actually read.
- Do not report text from trackside signage or the background.
- confidence is your overall certainty from 0.0 to 1.0."""

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


class VLMUnavailable(RuntimeError):
    pass


def check_available(settings: Settings) -> None:
    """Fail early and clearly rather than once per crop, mid-run."""
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


def describe(image: Image.Image | Path, settings: Settings, *, is_bike: bool = False,
             client: httpx.Client | None = None) -> VehicleDescription:
    """Ask the model what this vehicle is."""
    if isinstance(image, Path):
        image = Image.open(image)

    payload_image = _encode(image, settings.vlm_input_edge)
    payload = {
        "model": settings.vlm_model,
        "prompt": BIKE_PROMPT if is_bike else CAR_PROMPT,
        "images": [payload_image],
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0.0, "num_predict": 500},
    }

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.vlm_timeout)
    try:
        resp = client.post(f"{settings.vlm_host}/api/generate", json=payload,
                           timeout=settings.vlm_timeout)
        resp.raise_for_status()
        data = resp.json()
        body = data.get("response") or ""
        if not body.strip():
            # Reasoning models (qwen3-vl, for one) put the answer in
            # "thinking" and leave "response" empty even with think disabled.
            # Without this the app reads nothing back from them at all.
            body = data.get("thinking") or ""
    except Exception:
        return VehicleDescription()
    finally:
        if owns_client:
            client.close()

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return VehicleDescription()

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

    payload = {
        "model": settings.vlm_model,
        "prompt": BURST_PROMPT.format(count=len(encoded)),
        "images": encoded,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0.0, "num_predict": 400},
    }

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.vlm_timeout)
    try:
        resp = client.post(f"{settings.vlm_host}/api/generate", json=payload,
                           timeout=settings.vlm_timeout)
        resp.raise_for_status()
        data = resp.json()
        body = (data.get("response") or "").strip() or (data.get("thinking") or "")
        parsed = json.loads(body)
    except Exception:
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
