"""Recognising the same vehicle across frames, and agreeing on what it is.

A cruise or a track session gives you the same car many times over. The
vision model answers each crop independently, so one blue Ford Falcon came
back as a Fiesta, an Astra, a Commodore and a Mustang across eight frames --
each with confidence, none of them right, and no way for a reader of the grid
to tell which to believe.

Grouping the crops that look like the same vehicle and taking the majority
answer fixes the ones where the model was merely inconsistent. It does not
rescue a car the model gets wrong every time, and it does not pretend to: a
group reports how much of it agreed, so a 4-of-8 consensus can be told apart
from an 8-of-8 one.

The signature is deliberately cheap and local -- a difference hash for shape
and a coarse hue histogram for colour -- because it runs over every crop of a
six-thousand-frame shoot.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

HASH_EDGE = 8          # 8x9 grey samples -> 64 bits of shape
HUE_BINS = 12
SAT_BINS = 3


def signature(image: Image.Image) -> str:
    """A short string capturing rough shape and colour."""
    grey = image.convert("L").resize((HASH_EDGE + 1, HASH_EDGE), Image.LANCZOS)
    cells = np.asarray(grey, dtype=np.int16)
    bits = (cells[:, 1:] > cells[:, :-1]).flatten()
    packed = int("".join("1" if b else "0" for b in bits), 2)

    hsv = np.asarray(image.convert("HSV").resize((64, 64), Image.LANCZOS))
    hue = (hsv[:, :, 0].astype(np.float32) / 256.0 * HUE_BINS).astype(np.int32)
    sat = (hsv[:, :, 1].astype(np.float32) / 256.0 * SAT_BINS).astype(np.int32)
    bins = (np.clip(hue, 0, HUE_BINS - 1) * SAT_BINS
            + np.clip(sat, 0, SAT_BINS - 1)).ravel()
    hist = np.bincount(bins, minlength=HUE_BINS * SAT_BINS).astype(np.float32)
    hist /= max(1.0, hist.sum())
    return f"{packed:016x}:" + ",".join(f"{v:.4f}" for v in hist)


def _parse(sig: str) -> tuple[int, np.ndarray]:
    head, _, tail = sig.partition(":")
    return int(head, 16), np.array(tail.split(","), dtype=np.float32)


def _shape_distance(a: str, b: str) -> int:
    try:
        ha, _ = _parse(a)
        hb, _ = _parse(b)
    except Exception:
        return 999
    return bin(ha ^ hb).count("1")


def _colour_matches(a: str, b: str, min_colour: float) -> bool:
    try:
        _, ca = _parse(a)
        _, cb = _parse(b)
    except Exception:
        return False
    return float(np.minimum(ca, cb).sum()) >= min_colour


def similar(a: str, b: str, *, max_bits: int, min_colour: float) -> bool:
    """Do these two crops plausibly show the same vehicle?"""
    return (_colour_matches(a, b, min_colour)
            and _shape_distance(a, b) <= max_bits)


@dataclass
class Group:
    key: int
    members: list[int] = field(default_factory=list)
    signature: str = ""
    last_frame: int = 0


def cluster(rows: list[tuple[int, str, int]], *, max_bits: int = 14,
            min_colour: float = 0.62, frame_window: int = 6) -> dict[int, int]:
    """Assign each detection to a group. Returns detection id -> group key.

    Two signals, because neither is enough alone. Colour is stable across a
    panning sequence but says nothing about which blue car this is. Shape is
    discriminating but moves a lot as the car swings through the frame -- the
    eight frames of one Falcon split into three groups on shape alone.

    So: colour must match, and then either the shapes agree or the frames are
    near neighbours in the shoot. A burst of consecutive frames showing the
    same colour is nearly always one car going past.

    Compares against one representative per group rather than all pairs: a
    shoot has thousands of vehicles but dozens of distinct cars.
    """
    groups: list[Group] = []
    assignment: dict[int, int] = {}

    for det_id, sig, frame_index in rows:
        if not sig:
            continue
        for group in groups:
            if not _colour_matches(sig, group.signature, min_colour):
                continue
            shape_agrees = _shape_distance(sig, group.signature) <= max_bits
            nearby = abs(frame_index - group.last_frame) <= frame_window
            if shape_agrees or nearby:
                group.members.append(det_id)
                group.last_frame = max(group.last_frame, frame_index)
                assignment[det_id] = group.key
                break
        else:
            groups.append(Group(key=len(groups) + 1, members=[det_id],
                                signature=sig, last_frame=frame_index))
            assignment[det_id] = groups[-1].key
    return assignment


# Below this level of agreement the group has no answer, only a disagreement.
MIN_AGREEMENT = 0.5


@dataclass
class Consensus:
    make: str | None = None
    model: str | None = None
    colour: str | None = None
    race_number: str | None = None
    plate: str | None = None
    agreement: float = 0.0     # how much of the group backed the winning name
    size: int = 0
    disputed: list[str] = field(default_factory=list)


def _vote(values: list[str | None]) -> tuple[str | None, float]:
    present = [v.strip() for v in values if v and v.strip()]
    if not present:
        return None, 0.0
    counts = Counter(v.lower() for v in present)
    top, hits = counts.most_common(1)[0]
    # Give back the most common original spelling rather than the lowered key.
    for value in present:
        if value.lower() == top:
            return value, hits / len(present)
    return None, 0.0


def consensus(members: list[dict]) -> Consensus:
    """The group's agreed identity.

    Make and model are voted as one unit. Voting them separately produced
    "Holden Fiesta" out of a group that had said Ford Fiesta, Holden Astra and
    Holden Commodore -- a name no frame gave and no car has. A wrong answer
    that at least one reader actually proposed is recoverable; an invented one
    is not.

    Plates and race numbers are not voted at all: they are read rather than
    guessed, so the most confident single read wins.
    """
    out = Consensus(size=len(members))

    pairs = [((m.get("make") or "").strip(), (m.get("model") or "").strip())
             for m in members]
    named = [p for p in pairs if p[0] or p[1]]
    if named:
        counts = Counter((a.lower(), b.lower()) for a, b in named)
        (top_make, top_model), hits = counts.most_common(1)[0]
        for make, model in named:
            if (make.lower(), model.lower()) == (top_make, top_model):
                out.make, out.model = make or None, model or None
                break
        out.agreement = hits / len(named)
        if out.agreement < MIN_AGREEMENT:
            # Eight frames of one Falcon came back as a Fiesta, an Astra, a
            # Commodore and a Mustang. The majority of that is still noise, and
            # writing the plurality into someone's metadata would be worse than
            # writing nothing. Report the disagreement and let review settle it.
            out.disputed = sorted({f"{a} {b}".strip() for a, b in named})
            out.make = out.model = None

    out.colour, _ = _vote([m.get("colour") for m in members])

    for field_name in ("plate", "race_number"):
        best, best_conf = None, 0.0
        conf_key = "plate_conf" if field_name == "plate" else "number_conf"
        for m in members:
            value = m.get(field_name)
            conf = float(m.get(conf_key) or 0.0)
            if value and conf > best_conf:
                best, best_conf = value, conf
        setattr(out, field_name, best)
    return out


def consolidate(conn, job_id: int, settings=None) -> dict:
    """Group a finished job's vehicles and settle on one identity each.

    Runs over the stored crops, so it can be re-run after review without
    re-reading a single photo. The agreed identity is written back onto each
    detection, which means keywords, the review grid and the XMP all see the
    same answer without any of them knowing grouping exists.
    """
    import json

    rows = conn.execute(
        """SELECT d.id, d.crop_path, d.attributes, d.signature, i.id AS image_id
             FROM detections d JOIN images i ON i.id = d.image_id
            WHERE i.job_id = ? AND d.rejected = 0
            ORDER BY i.id, d.id""",
        (job_id,),
    ).fetchall()
    if not rows:
        return {"groups": 0, "vehicles": 0}

    signatures: list[tuple[int, str, int]] = []
    attributes: dict[int, dict] = {}
    for row in rows:
        sig = row["signature"]
        if not sig:
            try:
                with Image.open(row["crop_path"]) as crop:
                    crop.load()
                    sig = signature(crop)
            except Exception:
                continue
            conn.execute("UPDATE detections SET signature=? WHERE id=?",
                         (sig, row["id"]))
        signatures.append((row["id"], sig, row["image_id"]))
        attributes[row["id"]] = json.loads(row["attributes"] or "{}")

    assignment = cluster(signatures)
    members: dict[int, list[int]] = {}
    for det_id, key in assignment.items():
        members.setdefault(key, []).append(det_id)

    for key, ids in members.items():
        agreed = consensus([attributes.get(i, {}) for i in ids])
        for det_id in ids:
            current = attributes.get(det_id, {})
            # Only the identity is replaced. Plate and number stay as read on
            # this frame unless the frame has none, in which case the group's
            # best read is better than nothing.
            current["make"] = agreed.make
            current["model"] = agreed.model
            if agreed.colour:
                current["colour"] = agreed.colour
            if agreed.plate and not current.get("plate"):
                current["plate"] = agreed.plate
                current["plate_conf"] = 0.0     # inherited, not read here
            if agreed.race_number and not current.get("race_number"):
                current["race_number"] = agreed.race_number
            current["group_disputed"] = agreed.disputed
            conn.execute(
                """UPDATE detections
                      SET attributes=?, group_key=?, group_size=?, group_agreement=?
                    WHERE id=?""",
                (json.dumps(current), key, agreed.size,
                 round(agreed.agreement, 3), det_id),
            )
    conn.commit()
    return {"groups": len(members), "vehicles": len(signatures)}
