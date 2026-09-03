"""Is the subject actually in the frame?

A vehicle that runs off the edge of the photograph is a worse picture than
the same vehicle sitting inside it, however sharp the part you can see is.
Focus cannot tell you this -- a car cut in half at the frame edge is often
perfectly sharp -- so it is measured separately and applied to the rating
afterwards.

The measurement is the detector's box against the bounds of the frame. A box
that reaches the edge means the vehicle continues past it and the detector
had nowhere further to look; a box with clear air on every side means the
whole vehicle is present.

Two sides matter more than twice as much as one. A car touching the bottom
edge has lost its wheels; a car touching the bottom and the left has lost its
wheels and its nose, and the picture is usually unusable rather than merely
flawed. So the penalty grows with the share of the box's perimeter that lies
along the frame boundary, which is what "cut off on two sides" actually
means geometrically.

Deliberately not a model. The frame edge is a fact with coordinates, and
asking anything to infer it would be slower and worse.
"""

from __future__ import annotations

from dataclasses import dataclass

# How close to the edge still counts as touching it. Detector boxes rarely
# land exactly on the boundary, and a vehicle two pixels from the edge is cut
# off in every way that matters.
EDGE_TOLERANCE = 0.004        # share of the frame's width or height

# How hard clipping bites. Set so one edge is a blemish, two are serious and
# three leave a rating that will not survive the cull:
#   0 sides 1.00   1 side 0.85   2 sides 0.70   3 sides 0.55   4 sides 0.40
PENALTY = 0.6


@dataclass
class Framing:
    sides: int = 0            # how many frame edges the subject runs off
    factor: float = 1.0       # what to multiply the rating by

    @property
    def cut_off(self) -> bool:
        """Enough of the subject is missing to be worth saying out loud."""
        return self.sides >= 2


def assess(box, frame_width: int, frame_height: int) -> Framing:
    """How much of the subject the frame edge has taken."""
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return Framing()
    if frame_width <= 0 or frame_height <= 0:
        return Framing()

    margin_x = frame_width * EDGE_TOLERANCE
    margin_y = frame_height * EDGE_TOLERANCE
    sides = sum((
        x1 <= margin_x,
        y1 <= margin_y,
        x2 >= frame_width - margin_x,
        y2 >= frame_height - margin_y,
    ))
    return Framing(sides=sides, factor=1.0 - PENALTY * (sides / 4.0))


def describe(framing: Framing) -> str:
    """What to tell someone looking at the card."""
    if not framing.sides:
        return ""
    if framing.sides == 1:
        return "touches the frame edge"
    return f"cut off on {framing.sides} edges"
