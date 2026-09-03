"""Fixing a make that contradicts the model name beside it.

The vision model gets nameplates right far more often than it gets the badge
right. Asked about a Kawasaki Ninja H2 it answered make "Yamaha", model
"Ninja H2" -- the nameplate exactly correct, the marque simply wrong. The
same shape of error turns a Falcon into a Holden.

Only nameplates that belong to exactly one marque are listed. "Focus" is a
Ford and nothing else, so it can correct a make; "GT", "RS" and "Sport" are
sold by everyone and are not here. When the nameplate is unambiguous and the
stated make disagrees with it, the nameplate wins, because it is the part the
model demonstrably reads better.
"""

from __future__ import annotations

import re

# nameplate -> the only marque that sells it
NAMEPLATES: dict[str, str] = {
    # Australian-market cars, which is most of what comes past at these events
    "falcon": "Ford", "fairmont": "Ford", "territory": "Ford",
    "commodore": "Holden", "monaro": "Holden", "maloo": "Holden",
    "torana": "Holden", "kingswood": "Holden", "calais": "Holden",
    "statesman": "Holden", "sandman": "Holden",
    # common imports
    "focus": "Ford", "fiesta": "Ford", "mustang": "Ford", "ranger": "Ford",
    "impreza": "Subaru", "liberty": "Subaru", "forester": "Subaru",
    "brz": "Subaru", "wrx": "Subaru",
    "lancer": "Mitsubishi", "evolution": "Mitsubishi", "pajero": "Mitsubishi",
    "skyline": "Nissan", "silvia": "Nissan", "patrol": "Nissan",
    "supra": "Toyota", "corolla": "Toyota", "hilux": "Toyota",
    "landcruiser": "Toyota", "celica": "Toyota", "aurion": "Toyota",
    "civic": "Honda", "integra": "Honda", "nsx": "Honda", "accord": "Honda",
    "golf": "Volkswagen", "polo": "Volkswagen", "passat": "Volkswagen",
    "rx-7": "Mazda", "rx-8": "Mazda", "mx-5": "Mazda", "cosmo": "Mazda",
    "cooper": "MINI", "clubman": "MINI",
    # motorcycles
    "ninja": "Kawasaki", "zx-10r": "Kawasaki", "zx-6r": "Kawasaki",
    "z1000": "Kawasaki", "h2": "Kawasaki",
    "yzf": "Yamaha", "r1": "Yamaha", "r6": "Yamaha", "mt-09": "Yamaha",
    "hayabusa": "Suzuki", "gsx-r": "Suzuki", "gsxr": "Suzuki",
    "panigale": "Ducati", "monster": "Ducati", "multistrada": "Ducati",
    "fireblade": "Honda", "cbr": "Honda",
}

_WORD = re.compile(r"[a-z0-9][a-z0-9-]*")


def correct_make(make: str | None, model: str | None) -> str | None:
    """The make the model name implies, if it plainly contradicts the badge."""
    if not model:
        return make
    for token in _WORD.findall(model.lower()):
        owner = NAMEPLATES.get(token)
        if not owner:
            continue
        if not make or make.strip().lower() != owner.lower():
            return owner
        return make
    return make
