"""Is the subject sharp?

Not "is the photograph sharp" -- that question has the wrong answer for most
motorsport frames worth keeping. A panning shot is mostly blur on purpose:
the background is smeared, the wheels are rotating, and only the body of the
car is meant to be crisp. A whole-frame focus measure marks that as the worst
picture of the set and a static shot of a parked car as the best, which is
exactly backwards.

So this measures inside the vehicle crop, and inside the crop it looks for
the sharpest region rather than the average one:

  * the crop is divided into tiles;
  * each tile gets a focus measure normalised by its own contrast, so a dark
    car is not marked down for being dark;
  * the score is a high percentile of those tiles, not the mean.

That is what makes it survive the two cases that matter. Spinning wheels and
a smeared background drag the mean down while leaving the best tiles alone,
and a genuinely missed focus has no sharp tiles anywhere, so every tile is
low and the percentile is low with them.

Bokeh needs no special handling for the same reason: it is outside the crop,
and where a shallow depth of field blurs part of the car itself, the tiles on
the plane of focus still carry the answer.

The numbers here are a scale, not a truth. What counts as sharp depends on
the lens, the light and the photographer, so the thresholds are settings and
the raw score is stored alongside the verdict -- a shoot can be re-sorted
later without re-reading a single file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image

# Tiles across the longer edge. Enough that a rotating wheel is a minority of
# them, few enough that each still holds real detail at thumbnail sizes.
TILE_GRID = 6

# Which tile speaks for the crop. The maximum is too eager -- one specular
# highlight or a sharp-edged sticker on an otherwise soft car will hit it --
# so this takes the top of the distribution without taking its outlier.
TILE_PERCENTILE = 80

# Below this a tile is too flat to say anything: sky, a blown highlight, a
# plain door panel. Scoring them would let a big smooth car outvote its own
# sharp edges.
#
# Deliberately low. Set at 4.0 it excluded the tiles of a *badly* blurred
# crop, because blurring something flattens it -- so the worst frames came
# back "cannot tell" instead of "blurred", which is the one answer that is
# never useful.
MIN_TILE_CONTRAST = 1.5

# The crop is resized so the measure means the same thing on a 300px crop and
# a 3000px one. Without it a big crop scores higher for being big.
WORKING_EDGE = 512


@dataclass
class Sharpness:
    score: float = 0.0        # 0..1, the normalised focus measure
    verdict: str = "unknown"  # sharp | soft | blurred | unknown

    def __bool__(self) -> bool:
        return self.verdict != "unknown"


def _focus_map(grey: np.ndarray) -> np.ndarray:
    """Squared gradient energy per pixel (Tenengrad).

    Chosen over the variance of the Laplacian because the Laplacian is a
    second derivative and answers noise as loudly as it answers edges, which
    on a high-ISO frame reads as sharpness.
    """
    gx = np.zeros_like(grey)
    gy = np.zeros_like(grey)
    gx[:, 1:-1] = grey[:, 2:] - grey[:, :-2]
    gy[1:-1, :] = grey[2:, :] - grey[:-2, :]
    return gx * gx + gy * gy


def measure(image: Image.Image) -> Sharpness:
    """Score how sharp the subject of this crop is, from 0 to 1."""
    try:
        grey = image.convert("L")
    except (OSError, ValueError):
        return Sharpness()

    grey.thumbnail((WORKING_EDGE, WORKING_EDGE), Image.BILINEAR)
    data = np.asarray(grey, dtype=np.float32)
    if data.ndim != 2 or min(data.shape) < TILE_GRID * 4:
        return Sharpness()

    energy = _focus_map(data)
    rows = np.array_split(np.arange(data.shape[0]), TILE_GRID)
    cols = np.array_split(np.arange(data.shape[1]), TILE_GRID)

    scores: list[float] = []
    for row in rows:
        for col in cols:
            tile = data[row[0]:row[-1] + 1, col[0]:col[-1] + 1]
            contrast = float(tile.std())
            if contrast < MIN_TILE_CONTRAST:
                continue
            tile_energy = float(energy[row[0]:row[-1] + 1,
                                       col[0]:col[-1] + 1].mean())
            # Divided by contrast squared because the gradient energy scales
            # with the square of the amplitude of whatever is in the tile.
            # Without it this measures how contrasty the paint is, not how
            # well focused it was.
            scores.append(tile_energy / (contrast * contrast))

    if not scores:
        return Sharpness()

    raw = float(np.percentile(scores, TILE_PERCENTILE))
    return Sharpness(score=_normalise(raw), verdict="unknown")


# Measured across Gaussian blurs of a detailed target: a crisp crop lands
# near 4.0 and one blurred past saving near 0.15, with the useful judgements
# spread between. The spacing is close to logarithmic, so the scale is too --
# a hyperbola put everything from crisp to visibly soft inside four points of
# each other and made the score useless for sorting.
RAW_BLURRED, RAW_SHARP = 0.15, 4.0


def _normalise(raw: float) -> float:
    """Map the raw focus ratio onto 0..1 across the range that occurs."""
    if raw <= 0:
        return 0.0
    lo, hi = math.log(RAW_BLURRED), math.log(RAW_SHARP)
    return float(min(1.0, max(0.0, (math.log(raw) - lo) / (hi - lo))))


def verdict_for(score: float, sharp_at: float, blurred_below: float) -> str:
    if score >= sharp_at:
        return "sharp"
    if score < blurred_below:
        return "blurred"
    return "soft"


def rate(image: Image.Image, settings=None) -> Sharpness:
    """Measure a crop and label it against the configured thresholds."""
    result = measure(image)
    if not result and result.score <= 0:
        return result
    sharp_at = getattr(settings, "sharp_at", 0.62) if settings else 0.62
    blurred_below = getattr(settings, "blurred_below", 0.40) if settings else 0.40
    result.verdict = verdict_for(result.score, sharp_at, blurred_below)
    return result
