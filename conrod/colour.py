"""The actual colour of a vehicle, as a colour rather than as a word.

The vision model names a colour, and the name is often useless: a car that is
plainly teal comes back "blue", two cars that are nothing like each other both
come back "gray", and a dark green reads "black". The word is still worth
keeping -- it goes in the keywords -- but the review grid should show what the
paint actually looks like so a wrong one is obvious at a glance.

This samples the crop rather than asking anything. The hard part is that most
of a vehicle crop is not paint: glass and tyres are near-black, chrome and
sunlight are near-white, and the edges of the box are road, grass and sky.
"""

from __future__ import annotations

import colorsys

import numpy as np
from PIL import Image

# The middle of the box. Cars are photographed side-on at these events, so the
# top of the crop tends to be sky or trees, the bottom is road and shadow, and
# the far left and right are whatever was behind the car.
BODY_REGION = (0.15, 0.22, 0.85, 0.72)      # x1, y1, x2, y2 as fractions

MIN_VALUE = 45        # below this is glass, tyre or shadow, not paint
MAX_VALUE = 245       # above this is a specular highlight or blown sky
MIN_KEPT = 0.04       # if filtering leaves less than this, it filtered too hard
BAND_SHARE = 0.18     # a brightness band worth calling the paint


def dominant(image: Image.Image) -> str | None:
    """A hex colour representing the vehicle's paint, or None.

    Returns the median of the largest cluster rather than the mean of
    everything: averaging a red car against a grey road gives brown, which is
    both wrong and confidently wrong.

    Pixels are weighted towards the middle of the crop before anything is
    counted. Without that, a dark car photographed against the red-and-white
    kerbing sampled as brown -- the kerb was the most colourful thing in the
    box, and it was not the car.
    """
    if image is None:
        return None
    try:
        rgb = image.convert("RGB")
    except Exception:
        return None

    w, h = rgb.size
    if w < 8 or h < 8:
        return None
    x1, y1, x2, y2 = BODY_REGION
    body = rgb.crop((int(w * x1), int(h * y1), int(w * x2), int(h * y2)))
    if min(body.size) < 4:
        body = rgb
    body = body.resize((64, 64), Image.LANCZOS)

    pixels = np.asarray(body, dtype=np.uint8).reshape(-1, 3)
    hsv = np.asarray(body.convert("HSV"), dtype=np.uint8).reshape(-1, 3)
    weight = _centre_weight(64, 64).ravel()

    value = hsv[:, 2].astype(np.int16)
    keep = (value >= MIN_VALUE) & (value <= MAX_VALUE)
    if keep.mean() < MIN_KEPT:
        # A genuinely black or genuinely white car. Nothing survives the
        # filter, so describe everything rather than invent a mid-tone.
        keep = np.ones(len(value), dtype=bool)

    pixels, hsv, weight = pixels[keep], hsv[keep], weight[keep]
    saturation = hsv[:, 1].astype(np.int16)

    if _weighted_median(saturation, weight) < 40:
        # Black, white, silver, grey. Hue is meaningless here, and a plain
        # median mixes a white ute's panels with its own grille and shadow
        # and reports mid-grey. Cluster by brightness and take the biggest
        # band, which is the paint.
        return _hex(_biggest_band(pixels, value[keep], weight))

    colourful = saturation >= 40
    coloured, cw = pixels[colourful], weight[colourful]
    hues = hsv[colourful][:, 0].astype(np.float32) / 256.0

    # Hue is circular, so bin it and take the fullest bin with its neighbours
    # rather than a median that would put a red car halfway round the wheel.
    bins = 18
    index = np.clip((hues * bins).astype(np.int32), 0, bins - 1)
    counts = np.bincount(index, weights=cw, minlength=bins)
    peak = int(counts.argmax())
    near = np.isin(index, [(peak - 1) % bins, peak, (peak + 1) % bins])
    if near.sum() < 8:
        near = np.ones(len(index), dtype=bool)
    return _hex(_weighted_median_rgb(coloured[near], cw[near]))


def _centre_weight(w: int, h: int) -> np.ndarray:
    """Falls off towards the edges, where the background is."""
    ys = np.linspace(-1.0, 1.0, h)[:, None]
    xs = np.linspace(-1.0, 1.0, w)[None, :]
    return np.exp(-(xs ** 2 + ys ** 2) * 1.6).astype(np.float32)


def _biggest_band(pixels: np.ndarray, value: np.ndarray,
                  weight: np.ndarray) -> np.ndarray:
    bands = 8
    index = np.clip((value.astype(np.float32) / 256.0 * bands).astype(np.int32),
                    0, bands - 1)
    counts = np.bincount(index, weights=weight, minlength=bands)
    total = counts.sum()
    # The brightest band that is actually a substantial part of the crop,
    # not merely the biggest one. A white ute photographed head-on is mostly
    # windscreen, grille and shadow through the middle, so the biggest band
    # is dark and the answer came back grey. A black car has no substantial
    # bright band, so it still reads black.
    substantial = [i for i in range(bands)
                   if total > 0 and counts[i] / total >= BAND_SHARE]
    peak = max(substantial) if substantial else int(counts.argmax())
    near = np.isin(index, [max(0, peak - 1), peak, min(bands - 1, peak + 1)])
    if near.sum() < 8:
        near = np.ones(len(index), dtype=bool)
    return _weighted_median_rgb(pixels[near], weight[near])


def _weighted_median(values: np.ndarray, weight: np.ndarray) -> float:
    order = np.argsort(values)
    v, wt = values[order], weight[order]
    total = wt.sum()
    if total <= 0:
        return float(np.median(values)) if len(values) else 0.0
    return float(v[np.searchsorted(np.cumsum(wt), total / 2.0)
                   .clip(0, len(v) - 1)])


def _weighted_median_rgb(pixels: np.ndarray, weight: np.ndarray) -> np.ndarray:
    if not len(pixels):
        return np.array([0, 0, 0])
    return np.array([_weighted_median(pixels[:, i].astype(np.int16), weight)
                     for i in range(3)])


def _hex(values) -> str:
    r, g, b = (int(max(0, min(255, round(float(v))))) for v in values)
    return f"#{r:02x}{g:02x}{b:02x}"


def name_disagrees(hex_colour: str | None, word: str | None) -> bool:
    """Whether the sampled paint and the model's word are plainly different.

    Only used to flag a card for attention, so it is deliberately forgiving:
    "blue" against a teal sample is fine, "white" against a red one is not.
    """
    if not hex_colour or not word:
        return False
    try:
        r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, IndexError):
        return False
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    family = _family(hue * 360, light, sat)
    said = (word or "").strip().lower().split()[-1:]
    return bool(said) and family is not None and said[0] not in family


def _family(hue: float, light: float, sat: float) -> set[str] | None:
    if sat < 0.12:
        if light < 0.22:
            return {"black", "charcoal", "gunmetal", "dark"}
        if light > 0.78:
            return {"white", "pearl", "cream"}
        return {"silver", "grey", "gray", "gunmetal", "charcoal"}
    if hue < 15 or hue >= 345:
        return {"red", "maroon", "burgundy", "pink", "orange"}
    if hue < 45:
        return {"orange", "bronze", "gold", "brown", "tan", "beige", "red"}
    if hue < 70:
        return {"yellow", "gold", "lime", "cream"}
    if hue < 165:
        return {"green", "lime", "teal"}
    if hue < 200:
        return {"teal", "cyan", "aqua", "turquoise", "blue", "green"}
    if hue < 260:
        return {"blue", "navy", "teal", "purple"}
    if hue < 290:
        return {"purple", "violet", "blue", "magenta"}
    return {"pink", "magenta", "purple", "red"}
