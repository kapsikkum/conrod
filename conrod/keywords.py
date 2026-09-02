"""Turning what was read off a vehicle into keywords worth searching.

The goal is a keyword set a photographer would actually type into Lightroom's
search box: the number, the plate, the car, the team, the colour. Not every
token OCR happened to see.
"""

from __future__ import annotations

from .analyze import VehicleAnalysis
from .config import Settings
from .mapping import NumberMap


def for_vehicle(analysis: VehicleAnalysis, settings: Settings,
                number_map: NumberMap | None = None) -> list[str]:
    """Keywords for one detected vehicle."""
    prefix = settings.keyword_prefix
    out: list[str] = []

    def add(*values: str | None) -> None:
        for value in values:
            if value and str(value).strip():
                out.append(f"{prefix}{str(value).strip()}")

    # --- competition number, and whoever the grid says that is ---
    if analysis.race_number:
        number = analysis.race_number
        add(number, f"#{number}", f"Car {number}")
        if number_map:
            # The entry list is authoritative: if it knows this number, its
            # driver and team beat anything read off the panels.
            for keyword in number_map.keywords_for(number, prefix):
                if keyword not in out:
                    out.append(keyword)

    # --- registration ---
    if analysis.plate and settings.write_plate_keyword:
        add(analysis.plate)
        if analysis.plate_state:
            add(analysis.plate_state)

    # --- what the vehicle is ---
    add(analysis.make, analysis.colour, analysis.body_type)
    if analysis.make and analysis.model:
        # Both the bare model and the qualified one: someone searching "Focus
        # RS" and someone searching "Ford Focus RS" should each find it.
        add(analysis.model, f"{analysis.make} {analysis.model}")
    else:
        add(analysis.model)

    # --- affiliation ---
    # An uncorroborated team name is the model's guess with nothing read to
    # back it, so it stays out of the file until a human confirms it.
    if analysis.team and (analysis.team_corroborated
                          or analysis.number_source == "manual"):
        add(analysis.team)
    add(*analysis.sponsors)

    if analysis.is_competition:
        add("Motorsport")
    if analysis.is_bike:
        add("Motorcycle")

    seen: set[str] = set()
    ordered: list[str] = []
    for keyword in out:
        key = keyword.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(keyword)
    return ordered


def for_frame(analyses: list[VehicleAnalysis], settings: Settings,
              number_map: NumberMap | None = None) -> list[str]:
    """Keywords for a whole frame: the union across its vehicles."""
    seen: set[str] = set()
    ordered: list[str] = []
    for analysis in analyses:
        for keyword in for_vehicle(analysis, settings, number_map):
            key = keyword.casefold()
            if key not in seen:
                seen.add(key)
                ordered.append(keyword)
    return ordered


def caption_for(analyses: list[VehicleAnalysis]) -> str:
    """A one-line description, for writers who want a caption as well."""
    parts = [a.title for a in analyses if a.title]
    return "; ".join(parts)
