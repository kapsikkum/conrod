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
import colorsys
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import marques, normalise

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
    frames: set = field(default_factory=set)
    swatch: str | None = None
    cls: str | None = None
    make: str | None = None
    plates: set = field(default_factory=set)
    bursts: set = field(default_factory=set)


def cluster(rows: list[tuple], *, max_bits: int = 14,
            min_colour: float = 0.62, frame_window: int = 6,
            max_swatch: int = 52) -> dict[int, int]:
    """Assign each detection to a group. Returns detection id -> group key.

    Rows are (detection id, signature, frame index) and optionally sampled
    colour, detector class and make.

    The design follows what the signals are actually worth, measured on an
    eight-frame burst of one car against crops of different cars:

      shape (dHash)     same car 2..32, different cars 9..40. The two ranges
                        overlap almost entirely, so it cannot decide anything
                        on its own.
      colour histogram  scored ~1.00 for every pair, including a green ute
                        against a blue hatchback. It is computed over the
                        whole crop, so grass, track and sky swamp it.
      make              right on essentially every crop the vision model can
                        see at all, sharp or blurry.

    So the reliable signals do the gatekeeping and the weak ones only break
    ties. Four things must hold before two crops can be the same vehicle:

      * different photographs -- a car cannot appear twice in one frame;
      * the same detector class -- a motorcycle is not a car;
      * the same make, where both crops named one;
      * paint that actually matches.

    Only then does shape or frame proximity decide. Without the class and
    make gates, a motorbike and a silver SUV shot moments apart were merged
    and the bike's name was written over the car.

    A read plate overrides all of it, in both directions: same plate is the
    same vehicle, different plates are different vehicles. It is the only
    signal here that is an identity rather than a resemblance.
    """
    groups: list[Group] = []
    assignment: dict[int, int] = {}

    for row in rows:
        det_id, sig, frame_index = row[0], row[1], row[2]
        swatch = row[3] if len(row) > 3 else None
        cls = row[4] if len(row) > 4 else None
        make = row[5] if len(row) > 5 else None
        plate = _tidy_plate(row[6]) if len(row) > 6 else None
        burst = row[7] if len(row) > 7 else None
        if not sig:
            continue
        for group in groups:
            if frame_index in group.frames:
                continue

            # A plate is an identity, not a hint. Two crops showing the same
            # plate are one vehicle whatever the shape, colour or gap between
            # frames suggests, and two crops showing different plates are not
            # one vehicle however alike they look. This settles the case the
            # other signals kept getting wrong: a purple Falcon shot side-on
            # in sun and from behind in shade, read as the same plate both
            # times and as two different colours.
            # A burst is one unbroken run of the shutter at one subject, so
            # two of its frames are the same vehicle unless something
            # measured says otherwise. That makes it the strongest signal
            # here after an identical plate, and it has to be read before the
            # plate veto rather than after: one frame of this Jaguar read
            # ZE766 where eighteen read 39432J, and that single misread was
            # enough to declare six frames a different car.
            same_burst = burst is not None and burst in group.bursts

            verdict = _plate_verdict(plate, group.plates)
            if verdict is False and not same_burst:
                continue
            if verdict is True:
                _join(group, det_id, frame_index, make, plate, assignment, burst)
                break

            # A plate one confusable character away from one this group has
            # already seen. Not identity, but a measurement -- and it beats
            # the make, which is the vision model's opinion. Ranking them the
            # other way round is circular: the model called one blue Ford a
            # Holden, the make gate then refused the merge, and the eleven
            # frames that read it right never got to outvote it.
            near = _nearly_seen(plate, group.plates)

            if cls and group.cls and cls != group.cls:
                continue
            # The make gate is the vision model's opinion, and a burst is a
            # fact about when the shutter fired. Measured on one pass of a
            # silver Jaguar: thirty-one frames, one car, split into four
            # groups called Jaguar XJ-S, Jaguar XJS, Holden Monaro and Holden
            # HJ Torana -- because the frames it misread were refused entry
            # to the group that would have outvoted them.
            if not near and not same_burst and not _same_make(make, group.make):
                continue
            if not _colour_matches(sig, group.signature, min_colour):
                continue
            if not _swatch_matches(swatch, group.swatch, max_swatch):
                continue
            # Shape and frame proximity are resemblances, not identities, and
            # neither may cross a burst boundary on its own.
            #
            # Inside one run of the shutter, same_burst already says these
            # frames are one vehicle, so these two only ever *add* something
            # across bursts -- which is exactly where they are wrong. The
            # next car onto the same corner is photographed from the same
            # spot, at the same focal length, against the same fence,
            # seconds later: "within six frames" is true of two different
            # cars, and this module's own measurements say shape overlaps
            # almost entirely between same and different vehicles.
            #
            # Nothing else was stopping them. Before identify has run there
            # is no make and no sampled swatch, and both of those gates
            # abstain on a missing value rather than refusing -- deliberately,
            # so a crop too small to read can still join on other evidence.
            # With the detector class the only gate left, proximity chained a
            # Jaguar, a Mini and a black sedan into one 41-frame vehicle
            # across three bursts, and shape merged another thirteen frames
            # across two bursts minutes apart.
            #
            # So crossing a burst needs something that is an identity or an
            # agreed measurement: a near-matching plate, a make both sides
            # named, or a swatch both sides sampled. Absent values are not
            # agreement.
            crosses_burst = (burst is not None and bool(group.bursts)
                             and burst not in group.bursts)
            corroborated = bool(
                (make and group.make and _same_make(make, group.make))
                or (swatch and group.swatch
                    and _swatch_matches(swatch, group.swatch, max_swatch)))

            shape_agrees = _shape_distance(sig, group.signature) <= max_bits
            nearby = abs(frame_index - group.last_frame) <= frame_window
            if crosses_burst and not corroborated:
                shape_agrees = nearby = False

            if shape_agrees or nearby or near or same_burst:
                _join(group, det_id, frame_index, make, plate, assignment, burst)
                break
        else:
            groups.append(Group(key=len(groups) + 1, members=[det_id],
                                signature=sig, last_frame=frame_index,
                                frames={frame_index}, swatch=swatch,
                                cls=cls, make=make,
                                plates={plate} if plate else set(),
                                bursts={burst} if burst is not None else set()))
            assignment[det_id] = groups[-1].key
    return assignment


def _join(group: "Group", det_id: int, frame_index: int, make: str | None,
          plate: str | None, assignment: dict[int, int],
          burst: int | None = None) -> None:
    group.members.append(det_id)
    group.last_frame = max(group.last_frame, frame_index)
    group.frames.add(frame_index)
    group.make = group.make or make
    if plate:
        group.plates.add(plate)
    if burst is not None:
        group.bursts.add(burst)
    assignment[det_id] = group.key


def _tidy_plate(value: str | None) -> str | None:
    """A plate reduced to what can be compared: letters and digits only."""
    if not value:
        return None
    cleaned = "".join(c for c in str(value).upper() if c.isalnum())
    return cleaned or None


# Character pairs a plate reader confuses. Not a general edit distance: these
# are the substitutions that actually happen on a photographed plate, where
# the glyph is thirty pixels wide and half of it is motion blur.
CONFUSABLE = [set(x) for x in ("047", "8B", "0OQD", "1IL", "5S", "2Z", "6G",
                               "VY", "MN", "CG", "UV")]


def _near_plate(a: str, b: str) -> bool:
    """One character apart, and that character is one a reader confuses.

    A plate read as 43111J in one frame and 73111J in the next is one plate.
    Treating those as two was the single worst thing grouping did: because a
    plate is identity, the mismatch was proof of *different* vehicles, so one
    car panned across twenty-seven frames became four cars -- and the eleven
    frames that read it correctly never got to outvote the sixteen that did
    not.
    """
    if len(a) != len(b):
        return False
    differences = [(x, y) for x, y in zip(a, b) if x != y]
    if len(differences) != 1:
        return False
    x, y = differences[0]
    return any({x, y} <= pair for pair in CONFUSABLE)


def _nearly_seen(plate: str | None, seen: set) -> bool:
    """Whether this plate is one confusable character from one already seen."""
    if not plate or not seen:
        return False
    return any(_near_plate(plate, other) for other in seen)


def _plate_verdict(plate: str | None, seen: set) -> bool | None:
    """True: same vehicle. False: a different one. None: no opinion.

    A near miss is deliberately not False. It is not proof of the same
    vehicle either, so it abstains and lets the measured signals decide --
    see the near-plate branch in cluster().
    """
    if not plate or not seen:
        return None
    if plate in seen:
        return True
    if _nearly_seen(plate, seen):
        return None
    return False


def _same_make(a: str | None, b: str | None) -> bool:
    """Two crops may be one vehicle unless they named different makes.

    An absent make is not a mismatch -- a crop too small to read should still
    be allowed to join on the other evidence. The model is voted apart from
    the make on purpose: makes agree across a burst, model names do not, and
    correcting the model name is what grouping is for.
    """
    if not a or not b:
        return True
    return a.strip().lower() == b.strip().lower()


def _rgb(value: str) -> tuple[int, int, int] | None:
    try:
        return tuple(int(value[1 + i * 2:3 + i * 2], 16) for i in range(3))
    except (ValueError, IndexError, TypeError):
        return None


def _swatch_matches(a: str | None, b: str | None, limit: int) -> bool:
    """Whether two sampled paint colours are close enough to be one car.

    Compared by hue rather than by distance in RGB. One purple Falcon shot
    side-on in sun and from behind in shade sampled two colours far enough
    apart in RGB to be rejected as different cars, when only the brightness
    had moved -- the hue was the same purple in both.

    Absent on either side means "no opinion", not "no match": a crop the
    sampler could not read should still be allowed to join on other evidence.
    """
    if not a or not b:
        return True
    pa, pb = _rgb(a), _rgb(b)
    if pa is None or pb is None:
        return True

    ha, sa, va = colorsys.rgb_to_hsv(*[c / 255 for c in pa])
    hb, sb, vb = colorsys.rgb_to_hsv(*[c / 255 for c in pb])

    # Neither has a usable hue: black, white, silver, grey. Compare how light
    # they are instead, and generously, because exposure moves this a lot.
    if sa < 0.18 and sb < 0.18:
        return abs(va - vb) <= 0.45

    # One is coloured and the other is not. That is a real difference.
    if (sa < 0.18) != (sb < 0.18):
        return False

    apart = abs(ha - hb)
    apart = min(apart, 1.0 - apart)          # hue is a circle
    return apart <= HUE_TOLERANCE


# How far two hues may sit apart and still be called the same paint, of 1.0
# around the wheel. Purple in sun and purple in shade differ in brightness,
# not in hue.
HUE_TOLERANCE = 0.075

# Below this level of agreement the group has no answer, only a disagreement.
MIN_AGREEMENT = 0.5

# A make on its own is a weaker claim than a full name, so it needs a clearer
# majority before it is worth writing down.
MIN_MAKE_AGREEMENT = 0.6


@dataclass
class Consensus:
    make: str | None = None
    model: str | None = None
    colour: str | None = None
    race_number: str | None = None
    plate: str | None = None
    colour_hex: str | None = None   # sampled paint, not the model's word
    team: str | None = None
    sponsors: list[str] = field(default_factory=list)
    livery_text: list[str] = field(default_factory=list)
    agreement: float = 0.0     # how much of the group backed the winning name
    size: int = 0
    disputed: list[str] = field(default_factory=list)
    # True when the name came from looking at the pictures again rather than
    # from the per-frame readings, which is worth saying: it is a different
    # kind of answer and the review screen should not present it as a vote.
    second_look: bool = False


def _median_hex(values: list[str]) -> str | None:
    """The channel-wise median of several #rrggbb strings."""
    channels: list[list[int]] = [[], [], []]
    for value in values:
        try:
            for i in range(3):
                channels[i].append(int(value[1 + i * 2:3 + i * 2], 16))
        except (ValueError, IndexError):
            continue
    if not channels[0]:
        return None
    middle = [sorted(c)[len(c) // 2] for c in channels]
    return "#%02x%02x%02x" % tuple(middle)


# When one spelling is a misreading of another. Two conditions have to hold
# together -- close enough to be the same word, and rare enough against it to
# be a mistake rather than a second decal:
#
#   1 edit   4+ characters   3x commoner    "Betto"  against 19 "Betta"
#   2 edits  5+ characters   10x commoner   "Bella"  against 19 "Betta"
#
# The second rule is deliberately much stricter. Two edits reaches a lot of
# real words, so it is only allowed where the count makes a genuine second
# sponsor implausible. "Castrel" seen nine times against ten "Castrol" is
# two decals by this rule, and stays two.
VARIANT_RULES = ((1, 4, 3), (2, 5, 10))


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, abandoned once it passes ``limit``.

    Returns ``limit + 1`` for anything further apart, which is all the
    caller needs and saves walking the whole matrix for two unrelated names.
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1,          # deletion
                               current[j - 1] + 1,       # insertion
                               previous[j - 1] + (x != y)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _is_variant(text: str, count: int, kept: str, kept_count: int) -> bool:
    """Whether ``text`` is a misreading of the better-attested ``kept``."""
    for edits, min_length, ratio in VARIANT_RULES:
        if (min(len(text), len(kept)) >= min_length
                and count * ratio <= kept_count
                and _edit_distance(text, kept, edits) <= edits):
            return True
    return False


def _fold_variants(counts: Counter) -> Counter:
    """Merge misreadings of one decal into the spelling most frames agreed on.

    OCR off a moving car is not stable. One Mini's door was read as "Betta"
    nineteen times, "Betto" three times and "Bella" once -- one sponsor, and
    the card listed three, which pushed the real ones down the list and made
    the accumulated answer look like noise.

    Only the rare spelling moves, and only towards a much commoner one, so
    two sponsors that genuinely both appear are never merged into each other.
    """
    folded: Counter = Counter()
    # Commonest first, so variants always fold towards an established
    # spelling rather than whichever happened to be seen first.
    for text, count in counts.most_common():
        for kept in folded:
            if _is_variant(text, count, kept, folded[kept]):
                folded[kept] += count
                break
        else:
            folded[text] = count
    return folded


def _accumulate(members: list[dict], key: str) -> list[str]:
    """Every distinct string any frame in the group saw, commonest first.

    Order matters for keywords, and "seen in more frames" is the best
    available proxy for "actually on the car" rather than misread once.
    """
    counts: Counter = Counter()
    original: dict[str, str] = {}
    for member in members:
        seen = member.get(key) or []
        if isinstance(seen, str):
            seen = [seen]
        for value in seen:
            text = str(value).strip()
            if not text:
                continue
            counts[text.lower()] += 1
            original.setdefault(text.lower(), text)
    return [original[k] for k, _ in _fold_variants(counts).most_common()]


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

            # The full name is noise, but the make on its own may not be.
            # Eight frames of one blue Falcon came back as a Fairmont, a
            # Mustang, a Fiesta, an Astra and a Commodore -- yet six of the
            # eight said Ford. "Ford" is then a claim some frames actually
            # made and the rest mostly agree with, and unlike a voted pair it
            # cannot name a car that does not exist. The model is left empty
            # and the disagreement still shown, so review has the full story.
            makes = [a for a, _ in named if a]
            if makes:
                top, hits = Counter(m.lower() for m in makes).most_common(1)[0]
                if hits / len(named) >= MIN_MAKE_AGREEMENT:
                    out.make = next(m for m in makes if m.lower() == top)
                    out.agreement = hits / len(named)

    out.colour, _ = _vote([m.get("colour") for m in members])

    # The swatch is measured per frame, so take the middle one rather than
    # voting: a single frame where the crop caught mostly windscreen or kerb
    # is then outvoted by the rest of the group instead of standing alone.
    swatches = [m.get("colour_hex") for m in members if m.get("colour_hex")]
    if swatches:
        out.colour_hex = _median_hex(swatches)

    # Sponsor and livery text is *accumulated*, not voted. Each frame only
    # sees the panels facing the camera: one shot of a purple Falcon read
    # "CV Performance" off the door, the next read the number off the boot,
    # and neither is wrong. Taking a majority here would throw away whichever
    # side of the car was photographed less.
    out.team, _ = _vote([m.get("team") for m in members])
    out.sponsors = _accumulate(members, "sponsors")
    out.livery_text = _accumulate(members, "livery_text")

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


# How many frames of a burst are worth showing at once. Beyond a handful the
# answer stops improving and the call gets slow, and the frames after the
# best few are the ones the sharpness ranking put last for a reason.
SECOND_LOOK_FRAMES = 3


def _proposed_makes(members: list[dict]) -> set[str]:
    """Every make the frames' own readers actually named, folded for spelling."""
    out = set()
    for member in members:
        make = member.get("own_make") or member.get("make")
        if make:
            out.add(_plain(str(make)))
    return out


def _plain(text: str) -> str:
    """Letters and digits only, lowercased -- "Harley-Davidson" == "harley davidson"."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _second_look(ids: list[int], crops: dict, settings) -> "object":
    """Ask the vision model about the sharpest frames of one vehicle."""
    from . import vlm
    from pathlib import Path

    ranked = sorted((crops[i] for i in ids if i in crops),
                    key=lambda pair: pair[1], reverse=True)
    paths = [Path(path) for path, _ in ranked[:SECOND_LOOK_FRAMES] if path]
    if not paths:
        return vlm.VehicleDescription()
    return vlm.identify_burst(paths, settings)


def consolidate(conn, job_id: int, settings=None) -> dict:
    """Group a finished job's vehicles and settle on one identity each.

    Runs over the stored crops, so it can be re-run after review without
    re-reading a single photo. The agreed identity is written back onto each
    detection, which means keywords, the review grid and the XMP all see the
    same answer without any of them knowing grouping exists.
    """
    import json

    rows = conn.execute(
        """SELECT d.id, d.crop_path, d.attributes, d.signature, d.colour_hex,
                  d.cls, d.plate, d.sharpness, i.id AS image_id, i.burst_key
             FROM detections d JOIN images i ON i.id = d.image_id
            WHERE i.job_id = ? AND d.rejected = 0
            ORDER BY i.id, d.id""",
        (job_id,),
    ).fetchall()
    if not rows:
        return {"groups": 0, "vehicles": 0}

    signatures: list[tuple[int, str, int]] = []
    attributes: dict[int, dict] = {}
    crops: dict[int, tuple] = {}
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
        parsed = json.loads(row["attributes"] or "{}")
        # Vote on what each frame's own reader said, not on a previous
        # round's group answer, or a bad merge would reinforce itself.
        for field_name in ("make", "model", "colour"):
            if f"own_{field_name}" in parsed:
                parsed[field_name] = parsed[f"own_{field_name}"]
        parsed["colour_hex"] = row["colour_hex"]
        attributes[row["id"]] = parsed
        crops[row["id"]] = (row["crop_path"], row["sharpness"] or 0.0)
        signatures.append((row["id"], sig, row["image_id"], row["colour_hex"],
                           row["cls"], parsed.get("make"), row["plate"],
                           row["burst_key"]))

    assignment = cluster(signatures)
    members: dict[int, list[int]] = {}
    for det_id, key in assignment.items():
        members.setdefault(key, []).append(det_id)

    # One text-only model call per group, not per frame, and only where the
    # group actually disagreed with itself. See normalise.py -- the answer is
    # checked back against what was read before any of it is believed.
    tidy_names = bool(settings and getattr(settings, "normalise_names", False)
                      and getattr(settings, "use_vlm", False))
    look_again = bool(tidy_names and getattr(settings, "burst_second_look", True))
    name_cache: dict = {}

    for key, ids in members.items():
        group_members = [attributes.get(i, {}) for i in ids]
        agreed = consensus(group_members)
        if tidy_names:
            readings = normalise.readings_of(group_members)
            tidied = normalise.canonical(readings, settings, cache=name_cache)
            if tidied.make:
                agreed.make = tidied.make
                # The model belongs to the make it was chosen with. Keeping a
                # previous vote's model against a new make is how "Ford
                # Ninja H2" happens.
                agreed.model = tidied.model
            elif tidied.model:
                agreed.model = tidied.model

            # Still no model means the frames disagreed about which vehicle
            # this is, and no amount of rereading the words will settle it.
            # Show the model the pictures instead -- the sharpest of them,
            # because a badge smeared by a panning blur is what caused the
            # disagreement in the first place. See vlm.identify_burst.
            if look_again and not agreed.model and len(ids) > 1:
                seen = _second_look(ids, crops, settings)
                # The second look arbitrates between the makes the readers
                # proposed. It does not get to introduce a new one.
                #
                # Shown three frames of a red motorbike whose readers had said
                # Yamaha, it answered "Harley-Davidson" -- a marque no frame
                # had ever named -- and that went straight into the group.
                # An invented answer is worse than a wrong one, because a
                # wrong one at least came from looking at the photograph.
                proposed = _proposed_makes(group_members)
                if seen.make and proposed and _plain(seen.make) not in proposed:
                    # The model name belongs to the make it was chosen with,
                    # so a rejected make takes its model with it.
                    seen = None
                if seen is not None and (seen.make or seen.model):
                    agreed.make = seen.make or agreed.make
                    agreed.model = seen.model or agreed.model
                    # Nameplate beats badge: "Ninja H2" is a Kawasaki whatever
                    # the burst call put in the make field.
                    if agreed.model:
                        agreed.make = marques.correct_make(agreed.make,
                                                           agreed.model)
                    agreed.second_look = True
        for det_id in ids:
            current = attributes.get(det_id, {})
            # What this frame's reader said is a measurement and is never
            # overwritten. The group's answer is stored alongside it, and
            # everything downstream prefers the group's where there is one.
            #
            # This used to replace make and model in place. That made the
            # damage permanent: once a motorbike and a silver SUV were
            # wrongly grouped, the bike's name was written into the SUV's
            # own record, and no amount of regrouping afterwards could get
            # "Hyundai" back, because the evidence was gone.
            current.setdefault("own_make", current.get("make"))
            current.setdefault("own_model", current.get("model"))
            current.setdefault("own_colour", current.get("colour"))
            current["group_make"] = agreed.make
            current["group_model"] = agreed.model
            current["make"] = agreed.make or current.get("own_make")
            current["model"] = agreed.model or current.get("own_model")
            if agreed.colour:
                current["colour"] = agreed.colour
            if agreed.plate and not current.get("plate"):
                current["plate"] = agreed.plate
                current["plate_conf"] = 0.0     # inherited, not read here
            if agreed.race_number and not current.get("race_number"):
                current["race_number"] = agreed.race_number
            # Accumulated across the group: what one frame saw and another
            # could not, rather than a majority of what each saw alone.
            if agreed.team and not current.get("team"):
                current["team"] = agreed.team
            if agreed.sponsors:
                current["sponsors"] = agreed.sponsors
            if agreed.livery_text:
                current["livery_text"] = agreed.livery_text
            current["group_disputed"] = agreed.disputed
            current["group_second_look"] = agreed.second_look
            conn.execute(
                # group_colour_hex, never colour_hex. The per-frame sample
                # is a measurement and must survive: writing the group median
                # back over it destroyed the original, so a second regroup
                # then averaged values that were already averages, and five
                # different cars ended up sharing one colour to the byte.
                """UPDATE detections
                      SET attributes=?, group_key=?, group_size=?,
                          group_agreement=?, group_colour_hex=?
                    WHERE id=?""",
                (json.dumps(current), key, agreed.size,
                 round(agreed.agreement, 3), agreed.colour_hex, det_id),
            )
    conn.commit()
    return {"groups": len(members), "vehicles": len(signatures)}
