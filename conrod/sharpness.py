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

# The background is measured in tiles the same size as the subject's, not on
# a grid of its own. This used to be a fixed sixteen, which sounds harmless
# and was not: the per-tile figure is gradient energy over contrast squared,
# and that ratio depends on tile size. A small tile holds a higher fraction
# of whatever edge is in it, so a fine grid reports a systematically higher
# number than a coarse one for the same content, and comparing a 16-grid
# background against a 6-grid subject compares two different scales.
#
# It made the background of a panning shot score *above* its subject --
# median margin -0.126 where a held pan is supposed to run well positive --
# so pan detection had been dead for as long as it had existed: two frames
# in 1,720 of a shoot that is almost entirely panning. Sized to match, the
# same frames run +0.223 and 72 of 120 read as pans.
#
# The fixed grid was introduced because the crop is padded by less than a
# fifth, so a coarse tile almost always catches some of the car and nothing
# survives the overlap rule. That is still true and is still the constraint:
# where too few tiles survive the background is reported as unmeasured,
# which is honest, rather than measured on a scale that does not match.
BACKGROUND_MIN_TILES = 2

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

# How big the crop is when it is measured. Resized so the measure means the
# same thing on a 300px crop and a 3000px one; without it a big crop scores
# higher for being big. Raised from 512, which was
# quietly the largest fault in the cull: a saved crop runs to 2048px, so the
# vehicle inside it was being shrunk by four before anything looked at it,
# and a smear of twenty pixels came back as five. Two real frames of one
# shoot settle it -- a cleanly held pan scored 0.545 and a badly tracked one
# where the whole car is smeared scored 0.552, so the cull ranked the
# unusable frame above the keeper. Measured at 1024 the same pair separates
# by a factor of 1.29 the right way round.
#
# The cost is four times the pixels of a step that took about sixteen
# milliseconds, against a vision model that takes seconds. Worth it.
WORKING_EDGE = 1024


# How much sharper the subject has to be than what is behind it before the
# frame is called a pan rather than a miss. Measured on real frames: a held
# pan runs 0.2 to 0.5 above its background, and a frame where the whole
# picture moved runs within 0.05 either way.
PAN_MARGIN = 0.15

# Above this the background is not blurred enough for the frame to be a pan,
# whatever the subject is doing. Without it a sharp car against a sharp fence
# reads as a pan on the strength of a small difference.
PAN_BACKGROUND_CEILING = 0.55

# Bands along the vehicle's longer axis. Three is the useful number: a car
# has two ends and a middle, and the question being asked is whether the
# softness is confined to one end.
BANDS = 3

# How much higher one end has to score before it is worth saying that end is
# the sharp one, rather than that the car is evenly sharp.
END_MARGIN = 0.12

# A score this close to a threshold is not a judgement, it is a coin toss.
# Frames inside this band are still culled if they fall the wrong side, but
# they are flagged so a person sees them rather than losing them silently.
UNCERTAIN_MARGIN = 0.06

# Fewer usable tiles than this and the subject is too small or too flat to
# have been measured at all.
MIN_SUBJECT_TILES = 4


@dataclass
class Sharpness:
    score: float = 0.0        # 0..1, the normalised focus measure
    verdict: str = "unknown"  # sharp | soft | blurred | unknown

    # What is behind the subject. -1 means it was not measured, which is the
    # case whenever no box was given and the whole crop is the subject.
    background: float = -1.0

    # The subject is clearly sharper than its background: a held pan, which
    # is a keeper and must never be culled for the blur that makes it one.
    panning: bool = False

    # Which end of the vehicle carries the sharpness, in image terms:
    # "left", "right", or "even". Not front/back -- that needs to know which
    # way the car is pointing, and nothing at cull time does.
    sharp_end: str = "even"

    # Per-band scores along the longer axis, sharpest-end-first order kept as
    # measured (left to right, or top to bottom for a tall crop).
    bands: tuple = ()

    # The score sits close enough to a threshold that the verdict could have
    # gone either way. Culled all the same, but flagged for a person.
    uncertain: bool = False

    def __bool__(self) -> bool:
        return self.verdict != "unknown"

    @property
    def partly_sharp(self) -> bool:
        """One end of the car is sharp even though the score overall is not.

        A panner focused on the nose with a smeared tail is a picture; the
        same score spread evenly over the whole car is a missed frame.
        """
        return bool(self.bands) and max(self.bands) - min(self.bands) >= END_MARGIN


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


def _tile_scores(data: np.ndarray, grid: int = TILE_GRID) -> list[float]:
    """The per-tile focus ratios of one region, skipping the featureless ones."""
    if data.ndim != 2 or min(data.shape) < grid * 4:
        return []
    energy = _focus_map(data)
    rows = np.array_split(np.arange(data.shape[0]), grid)
    cols = np.array_split(np.arange(data.shape[1]), grid)

    scores: list[float] = []
    for row in rows:
        for col in cols:
            if not len(row) or not len(col):
                continue
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
    return scores


def _region_score(data: np.ndarray, grid: int = TILE_GRID) -> float:
    scores = _tile_scores(data, grid)
    if not scores:
        return -1.0
    return _normalise(float(np.percentile(scores, TILE_PERCENTILE)))


def _greyscale(image: Image.Image):
    try:
        grey = image.convert("L")
    except (OSError, ValueError):
        return None
    return grey


def measure(image: Image.Image, box=None) -> Sharpness:
    """Score how sharp the subject is, and say what is behind it.

    ``box`` is the detector's box in the coordinates of ``image``. Given one,
    everything is measured on the vehicle and the rest of the crop is scored
    separately as background -- which is the only way to tell a held pan from
    a missed frame, because they differ in where the sharpness is and not in
    how much of it there is.

    Without a box the whole crop is treated as the subject, which is the old
    behaviour and is wrong in both directions: a car against a sharp fence
    scored the fence, and a panner scored its own smeared background.
    """
    grey = _greyscale(image)
    if grey is None:
        return Sharpness()

    # Scale the box with the image, so the measure means the same thing on a
    # 300px crop and a 3000px one.
    scale = min(1.0, WORKING_EDGE / max(grey.size)) if max(grey.size) else 1.0
    grey.thumbnail((WORKING_EDGE, WORKING_EDGE), Image.BILINEAR)
    data = np.asarray(grey, dtype=np.float32)
    if data.ndim != 2 or min(data.shape) < TILE_GRID * 4:
        return Sharpness()

    height, width = data.shape
    inner = None
    if box is not None:
        x1, y1, x2, y2 = (float(v) * scale for v in box)
        x1, y1 = max(0.0, x1), max(0.0, y1)
        x2, y2 = min(float(width), x2), min(float(height), y2)
        if x2 - x1 >= TILE_GRID * 4 and y2 - y1 >= TILE_GRID * 4:
            inner = (int(x1), int(y1), int(x2), int(y2))

    if inner is None:
        score = _region_score(data)
        if score < 0:
            return Sharpness()
        return Sharpness(score=score, verdict="unknown")

    x1, y1, x2, y2 = inner
    subject = data[y1:y2, x1:x2]
    subject_tiles = _tile_scores(subject)
    if len(subject_tiles) < MIN_SUBJECT_TILES:
        # Too little of the vehicle carries any detail to measure. Falling
        # back to the whole crop is better than refusing to answer, but the
        # answer is about the picture rather than the car, so say so.
        score = _region_score(data)
        if score < 0:
            return Sharpness()
        return Sharpness(score=score, verdict="unknown", uncertain=True)

    score = _normalise(float(np.percentile(subject_tiles, TILE_PERCENTILE)))

    # What is behind it. Everything outside the box, which on a pan is the
    # smear that makes the picture and on a missed frame is often the only
    # sharp thing in it.
    background = _background_score(data, inner)

    panning = (background >= 0.0
               and background <= PAN_BACKGROUND_CEILING
               and score - background >= PAN_MARGIN)

    bands, sharp_end = _bands(subject)
    return Sharpness(score=score, verdict="unknown", background=background,
                     panning=panning, bands=bands, sharp_end=sharp_end)


def _background_score(data: np.ndarray, inner) -> float:
    """Focus of the parts of the crop the vehicle is not in.

    Tiles that straddle the edge of the box are dropped rather than assigned:
    they hold both the car and what is behind it, and on a pan that is the
    one place the two cannot be told apart.

    In tiles the size of the subject's, so that the two numbers mean the
    same thing and can be subtracted -- see BACKGROUND_MIN_TILES for what
    went wrong when they were not.
    """
    x1, y1, x2, y2 = inner
    height, width = data.shape
    # The subject's own tile size, in pixels, which is the unit the two
    # scores have to share.
    tile_px = max(y2 - y1, x2 - x1) / TILE_GRID
    if tile_px <= 0:
        return -1.0
    rows = np.array_split(np.arange(height), max(2, round(height / tile_px)))
    cols = np.array_split(np.arange(width), max(2, round(width / tile_px)))
    energy = _focus_map(data)

    scores: list[float] = []
    for row in rows:
        for col in cols:
            if not len(row) or not len(col):
                continue
            r0, r1, c0, c1 = row[0], row[-1] + 1, col[0], col[-1] + 1
            # Any overlap with the vehicle at all disqualifies the tile.
            if not (c1 <= x1 or c0 >= x2 or r1 <= y1 or r0 >= y2):
                continue
            tile = data[r0:r1, c0:c1]
            contrast = float(tile.std())
            if contrast < MIN_TILE_CONTRAST:
                continue
            scores.append(float(energy[r0:r1, c0:c1].mean())
                          / (contrast * contrast))

    if len(scores) < BACKGROUND_MIN_TILES:
        return -1.0
    return _normalise(float(np.percentile(scores, TILE_PERCENTILE)))


def _bands(subject: np.ndarray):
    """Sharpness along the vehicle's longer axis, and which end wins.

    Reported in image terms -- left and right, or top and bottom for a tall
    crop. Deliberately not "front" and "back": that needs to know which way
    the car is pointing, and nothing available at cull time does.
    """
    height, width = subject.shape
    horizontal = width >= height
    length = width if horizontal else height
    if length < BANDS * TILE_GRID * 2:
        return (), "even"

    edges = np.linspace(0, length, BANDS + 1).astype(int)
    scores = []
    for start, end in zip(edges, edges[1:]):
        piece = subject[:, start:end] if horizontal else subject[start:end, :]
        # A coarser grid inside a band, which is a third of the vehicle.
        scores.append(_region_score(piece, grid=max(2, TILE_GRID // 2)))
    if any(s < 0 for s in scores):
        return (), "even"

    bands = tuple(round(s, 3) for s in scores)
    if max(bands) - min(bands) < END_MARGIN:
        return bands, "even"
    first, last = bands[0], bands[-1]
    if abs(first - last) < END_MARGIN:
        return bands, "middle" if bands[1] == max(bands) else "even"
    if horizontal:
        return bands, "left" if first > last else "right"
    return bands, "top" if first > last else "bottom"


# Measured across Gaussian blurs of a real vehicle crop, at WORKING_EDGE:
# crisp lands at 0.82, one pixel of blur at 0.68, three at 0.34, and by eight
# it is 0.10 and past saving. The spacing is close to logarithmic, so the
# scale is too -- a hyperbola put everything from crisp to visibly soft
# inside four points of each other and made the score useless for sorting.
#
# Re-derived when WORKING_EDGE changed. These constants are a statement about
# how much focus energy survives a given blur at a given resolution, so they
# do not carry across a change of scale: left at 0.15/4.0 the whole shoot
# collapsed into the bottom third of the range.
#
# The floor is where a frame stops being worth ranking rather than where it
# stops being an image. Putting it at the twelve-pixel mark instead pinned
# two frames in five at exactly zero, which throws away the ordering that
# sorting worst-first depends on.
RAW_BLURRED, RAW_SHARP = 0.10, 0.85


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


def rating_for(score: float, sharp_at: float, blurred_below: float) -> str:
    """The same bands, named for a picture rather than for its focus.

    A vehicle cut in half by the frame edge is frequently pin sharp, so the
    combined rating cannot borrow the focus words without lying about why a
    frame scored badly.
    """
    if score >= sharp_at:
        return "good"
    if score < blurred_below:
        return "poor"
    return "fair"


# The rating, as a photographer's catalogue understands it. Conrod judges the
# subject and the framing; the catalogue is where that judgement is acted on,
# so it has to arrive as stars and a colour label rather than as a private
# score in a private database.
#
# Where the subject rating lands on the one-to-five scale.
#
# The measure used to stop at four, on the grounds that whether the moment
# is any good is not a focus measurement. True, but it is the photographer's
# scale and they can always overrule it -- so the cull gives its honest
# opinion across the whole range rather than holding one back.
#
# Fitted against 944 frames of one Bathurst weekend that the photographer
# rated by hand, one to five, rather than against a synthetic target. The
# floors are the points that reproduce their own spread -- 62% of the shoot
# on one star, 19% on two, 9% on three, 7% on four and 2% on five -- which
# is a harsher standard than any curve fitted to a blur ladder produced, and
# it is theirs.
#
# On those 944 frames this agrees exactly 60% of the time and lands within
# one star 89% of the time, against 58% and 88% for the measure it replaces.
# The honest reading of that gap is that it is small: most of what a cull
# can contribute is already in the ordering, and the remaining disagreement
# is not blur. Whether a frame is worth keeping also turns on which car it
# is and what the light was doing, and a focus measure cannot see either --
# so these bands are a first pass to sort by, not a verdict.
#
# Absolute thresholds rather than a per-album ranking, deliberately: a frame
# should not change rating because of what else is in the album with it, and
# two albums should be comparable. Fitted on one album, then fixed.
STAR_BANDS = ((0.958, 5), (0.825, 4), (0.728, 3), (0.606, 2), (0.0, 1))

LABEL_GOOD, LABEL_FAIR, LABEL_POOR = "Green", "Yellow", "Red"

# The one frame of a pass worth keeping, and the only label that is not
# about focus. It wins over the others because it is a stronger statement:
# green says this frame is sharp, blue says this is *the* sharp one of the
# twelve you shot of that car.
#
# A colour rather than a flag because a flag does not survive the trip.
# Lightroom's Pick flag lives in the catalogue and is not written to a
# sidecar; xmp:Label is, and every catalogue reads it.
LABEL_PICK = "Blue"

# The good/fair/poor wording, kept on the same footing as the stars so the
# card cannot say "good" about a frame it is giving two stars to -- which is
# exactly what it did when these were tuned against a scale that no longer
# exists. Good is the four-star floor, poor is anything under two.
#
# They live in config beside the setting they are the default for, because
# config is imported long before there is any reason to pay for numpy and
# Pillow. FOCUS_SCALE is bumped whenever the scale above is re-derived, and
# Settings.load uses it to retire hand-set thresholds that were about the
# old one.
from .config import BLURRED_BELOW, FOCUS_SCALE, SHARP_AT  # noqa: E402,F401


def stars_for(rating: float) -> int:
    """Subject rating on the one to five scale every catalogue shares."""
    for floor, stars in STAR_BANDS:
        if rating >= floor:
            return stars
    return 1


def label_for(rating_verdict: str) -> str:
    """The colour a culled frame turns, and the one a keeper turns.

    Red for what the cull dropped, green for what it kept: filterable in
    Lightroom without knowing anything about how the number was arrived at.
    """
    if rating_verdict == "good":
        return LABEL_GOOD
    if rating_verdict == "poor":
        return LABEL_POOR
    return LABEL_FAIR


def rate(image: Image.Image, settings=None, box=None) -> Sharpness:
    """Measure a crop and label it against the configured thresholds."""
    result = measure(image, box)
    if not result and result.score <= 0:
        return result
    sharp_at = getattr(settings, "sharp_at", SHARP_AT) if settings else SHARP_AT
    blurred_below = (getattr(settings, "blurred_below", BLURRED_BELOW)
                     if settings else BLURRED_BELOW)
    result.verdict = verdict_for(result.score, sharp_at, blurred_below)
    return result


def doubtful(result: "Sharpness", rating_verdict: str,
             blurred_below: float = 0.25) -> bool:
    """Whether a decision to cull this frame was a close one.

    Deliberately only asked about frames being culled. Every measurement is
    uncertain somewhere, and flagging all of them buries the ones that matter:
    on a real shoot, "close to some threshold" was two thirds of the frames
    and "close to being thrown away for the wrong reason" was three percent.
    """
    if rating_verdict != "poor":
        return False
    if result.uncertain:                       # the subject could not be measured
        return True
    if abs(result.score - blurred_below) < UNCERTAIN_MARGIN:
        return True
    # One end sharp and the other smeared is what a pan looks like from the
    # side. Averaged over the whole car that reads "blurred", and the frame
    # the photographer went there to take is the one that gets binned.
    return result.partly_sharp


def cullable(result: "Sharpness", rating_verdict: str) -> bool:
    """Whether this may be culled automatically at all.

    A held pan is the picture the photographer went there for: the background
    is smeared on purpose and the subject is not. Culling it for being blurred
    is the worst thing an automatic cull can do, so a pan is never dropped
    without a person seeing it -- however low the number is.
    """
    if rating_verdict != "poor":
        return False
    return not result.panning
