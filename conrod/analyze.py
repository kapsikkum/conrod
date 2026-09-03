"""Per-vehicle analysis.

Everything happens on the crop of one detected vehicle, never the whole frame.
That is what stops trackside signage — the GAZOO RACING banners at Mount
Panorama, for instance — from being keyworded onto every car that drives past
them, and it is what lets a number and a team be attributed to a specific car
in a pack shot.

Four readers contribute, each doing the thing it is actually good at:

    plate detector + OCR   registration, at native crop resolution
    OCR over the crop      race number and large livery text
    vision model           make, model, colour, body type, team, sponsors
    merge                  reconcile the number, drop the plate from the text
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import httpx
from PIL import Image

from . import ocr, plates, vlm
from .config import Settings


@dataclass
class VehicleAnalysis:
    # what it is
    kind: str = "car"                     # detector class
    is_bike: bool = False
    make: str | None = None
    model: str | None = None
    colour: str | None = None
    body_type: str | None = None

    # identity
    plate: str | None = None
    plate_state: str | None = None
    plate_conf: float = 0.0
    race_number: str | None = None
    number_source: str | None = None
    number_conf: float = 0.0

    # affiliation
    team: str | None = None
    team_corroborated: bool = False
    sponsors: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    is_competition: bool = False

    vlm_conf: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> "VehicleAnalysis":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return cls()
        analysis = cls()
        for key, value in data.items():
            if hasattr(analysis, key):
                setattr(analysis, key, value)
        return analysis

    @property
    def title(self) -> str:
        """A short human label for the review UI."""
        # The model often already carries the make ("Holden Commodore"), which
        # rendered as "blue Holden Holden Commodore" in the grid.
        model = self.model or ""
        make = self.make or ""
        if make and model.lower().startswith(make.lower()):
            make = ""
        # Sentence case on the colour alone. The model returns it lowercase
        # ("blue Ford Fairmont"), which read as a typo next to the proper
        # nouns beside it. Only the first character, so "dark metallic blue"
        # does not lose its other words to .capitalize().
        colour = self.colour or ""
        colour = colour[:1].upper() + colour[1:]
        bits = [b for b in (colour, make, model) if b]
        label = " ".join(bits) if bits else self.kind
        if self.race_number:
            label = f"#{self.race_number} {label}"
        return label


def analyze(crop: Image.Image, settings: Settings, *, is_bike: bool = False,
            kind: str = "car", client: httpx.Client | None = None,
            native: Image.Image | None = None) -> VehicleAnalysis:
    """Read everything readable off one vehicle crop."""
    analysis = VehicleAnalysis(kind=kind, is_bike=is_bike)

    # 1. Registration, and any number roundels the same detector picked up.
    #    OCR alone never finds a plate in a natural scene and the vision model
    #    cannot resolve the characters, so this needs its own detector.
    roundel_numbers: list[tuple[str, float]] = []
    if settings.read_plates:
        reading, roundel_numbers = plates.scan_regions(crop, settings,
                                                       native=native)
        if reading.text:
            analysis.plate = reading.text
            analysis.plate_state = reading.state
            analysis.plate_conf = reading.confidence

    # 2. Competition number. A roundel read wins over whole-crop OCR: it comes
    #    from a localised, upscaled region rather than the entire car.
    ocr_number = ocr.Reading(None, 0.0)
    if settings.read_numbers:
        if roundel_numbers:
            token, score = roundel_numbers[0]
            ocr_number = ocr.Reading(token, min(1.0, score + 0.15), "roundel")
        else:
            ocr_number = ocr.read_number(crop, settings)
        # A registration read as a number would be wrong in a way that is hard
        # to spot later, so anything plate-shaped is discarded here.
        if ocr_number.number and (
            ocr_number.number == analysis.plate
            or plates.looks_like_plate(ocr_number.number)
        ):
            ocr_number = ocr.Reading(None, 0.0)

    # 3. The semantic pass.
    described = vlm.VehicleDescription()
    if settings.use_vlm:
        described = vlm.describe(crop, settings, is_bike=is_bike, client=client)
        analysis.vlm_conf = described.confidence
        if settings.identify_make_model:
            analysis.make, analysis.model = described.make, described.model
            analysis.body_type = described.body_type
        if settings.identify_colour:
            analysis.colour = described.colour
        if settings.identify_team:
            analysis.team = described.team
            analysis.sponsors = described.sponsors
        analysis.is_competition = described.is_competition

    # 4. Reconcile the number.
    vlm_number = described.race_number
    # The model will put a competition number on a road car if asked hard
    # enough — a NSW highway patrol wagon came back as "#220". When it has
    # already said this is not a competition vehicle, its own number claim is
    # not evidence; a roundel detection or a confident OCR read still is.
    if (vlm_number and not described.is_competition
            and not roundel_numbers
            and ocr_number.number != vlm_number):
        vlm_number = None

    analysis.race_number, analysis.number_source, analysis.number_conf = _merge_number(
        ocr_number, vlm_number, settings
    )

    # 5. Free text, minus anything already captured as a field.
    if settings.read_text:
        claimed = {
            *(s.upper() for s in analysis.sponsors),
            *(t.upper() for t in (analysis.team,) if t),
        }
        if analysis.plate:
            claimed.add(analysis.plate.upper())
        if analysis.plate_state:
            claimed.add(analysis.plate_state.upper())
        if analysis.race_number:
            claimed.add(analysis.race_number.upper())

        seen = {"".join(c for c in s if c.isalnum()) for s in claimed}
        found = ocr.visible_text(crop, settings, exclude=seen)
        # The model's livery reading and OCR's often overlap; keep the union.
        merged: list[str] = []
        for item in list(described.livery_text) + found:
            key = "".join(c for c in item.upper() if c.isalnum())
            if key and key not in seen:
                seen.add(key)
                merged.append(item)
        analysis.text = merged[: settings.max_text_items]

        # The model will occasionally invent a team from OCR noise — a Mini at
        # Bathurst produced "Nosso" out of the garbled tokens "Nos8e"/"No886".
        # A team name that no read text supports is kept but marked, so the
        # review UI can surface it instead of writing fiction into XMP.
        # Only OCR counts as evidence. The model's own livery_text would
        # happily "confirm" its own invention, which proves nothing.
        analysis.team_corroborated = _corroborated(analysis.team, found)

    return analysis


def _corroborated(claim: str | None, evidence: list[str]) -> bool:
    """Is a model-reported name actually supported by text that was read?"""
    if not claim:
        return False
    haystack = "".join(c for c in " ".join(evidence).upper() if c.isalnum())
    if not haystack:
        return False
    # Match on the distinctive words only; "Racing" or "Team" corroborate nothing.
    generic = {"RACING", "TEAM", "MOTORSPORT", "MOTORSPORTS", "AUTO", "GARAGE"}
    words = [
        "".join(c for c in word.upper() if c.isalnum())
        for word in claim.split()
    ]
    words = [w for w in words if len(w) >= 4 and w not in generic]
    if not words:
        return False
    return any(word in haystack for word in words)


def _merge_number(ocr_reading: "ocr.Reading", vlm_number: str | None,
                  settings: Settings) -> tuple[str | None, str | None, float]:
    """Decide the competition number from two disagreeing readers."""
    ocr_number = ocr_reading.number

    source = ocr_reading.source or "ocr"

    if ocr_number and vlm_number and ocr_number == vlm_number:
        # Independent agreement is worth more than either one's confidence.
        return ocr_number, f"{source}+vlm", min(
            1.0, max(ocr_reading.confidence, 0.7) + 0.2
        )

    if ocr_number and ocr_reading.confidence >= settings.ocr_accept_confidence:
        return ocr_number, source, ocr_reading.confidence

    if vlm_number:
        # The model reads stylised and angled numbers far better than OCR, so
        # it wins where OCR was not already confident.
        return vlm_number, "vlm", 0.7

    if ocr_number:
        # Weak, but better than nothing — the low score sends it to review.
        return ocr_number, source, ocr_reading.confidence

    return None, None, 0.0
