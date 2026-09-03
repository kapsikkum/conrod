"""Settling a group's noisy readings into one canonical name.

Grouping gets eleven frames of the same car to agree that they are the same
car. It does not make them agree on what to *call* it. Across a panning burst
one Falcon comes back as "Falcon", "Ford Falcon FG", "FG XR6", "Fairmont" and
"Ford Falcon FG MkII", and a majority vote picks whichever spelling happened
to win, not the right one.

So this asks the model that is already installed. It is a text-only call --
no image, no crop, no GPU decode -- and it runs once per *group*, not once
per frame: forty-one calls for a six-thousand-frame shoot, a second or two
each.

The interesting part is what it is not allowed to do. A language model asked
to tidy up car names will happily invent a plausible one, and a confident
wrong name is worse than an untidy right one, because it survives into the
XMP and out to a client. So the answer is checked back against the readings:

  * the make must be one that was actually read, or the marque that
    marques.py maps an observed nameplate to;
  * every word of the model must appear in something that was actually read;
  * anything else is dropped and the group keeps the name it voted for.

That reduces the model's job to choosing and tidying among things that were
genuinely seen, which is what it is good at, and removes the job it is bad
at -- knowing what car this is.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

import httpx

from . import marques
from .config import Settings

PROMPT = """You are tidying up vehicle identifications made by a computer
vision system. It looked at several photographs of the SAME vehicle and
described it separately each time, so the readings below disagree with each
other in wording, spelling and detail.

Readings of this one vehicle:
{readings}

Give the single best canonical identification.

Rules, and they matter more than being helpful:
- Use ONLY information present in the readings above. Never introduce a make,
  model or series that does not appear there.
- "make" is the manufacturer alone (Ford, Holden, Nissan, Kawasaki).
- "model" is the nameplate and series as normally written (Falcon FG,
  Commodore VE, Skyline R34, Ninja H2). Do not repeat the make in it.
- The frame counts are your best evidence. A reading seen in most of the
  frames is what the vehicle is; one seen once or twice is a misread, however
  plausible it sounds on its own.
- Prefer the most specific reading that several readings support. A detail
  only one reading mentions is probably wrong.
- If the readings agree on the nameplate but disagree on the series or
  variant ("Commodore VC" against "Commodore VX"), keep the nameplate and
  drop the series. Half an answer that is right beats a whole one that is a
  coin toss.
- Only if the readings disagree about the nameplate itself ("Fairmont"
  against "Fiesta") return null for model and give just the make they share.
  Returning null there is correct and useful. Guessing is not.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "make": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "colour": {"type": ["string", "null"]},
        "confident": {"type": "boolean"},
    },
    "required": ["make", "model", "colour", "confident"],
}


@dataclass
class Reading:
    """One distinct thing the per-frame readers said, and how often.

    The count is the whole point. Deduplicating the readings and handing the
    model a plain list threw it away, and on a real burst of twenty frames of
    one Jaguar -- seventeen of which read "Jaguar XJ-S" or "Jaguar XJS", and
    three "Nissan Fairlady Z" or "Nissan 240Z" -- the model had no way to
    know which was the outlier and chose the Nissan.
    """
    make: str = ""
    model: str = ""
    count: int = 1

    @property
    def text(self) -> str:
        return " ".join(b for b in (self.make, self.model) if b)


def readings_from(texts: "list[str]") -> "list[Reading]":
    """Readings from bare strings, each seen once. For callers without tallies."""
    out = []
    for text in texts:
        bits = str(text).split(" ", 1)
        out.append(Reading(make=bits[0], model=bits[1] if len(bits) > 1 else ""))
    return out


@dataclass
class Canonical:
    make: str | None = None
    model: str | None = None
    rejected: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.make or self.model)


def readings_of(members: list[dict]) -> list[str]:
    """What each frame's own reader said, one line per distinct reading.

    Deduplicated, because ten identical readings and one outlier should not
    read to the model as a ten-to-one vote it needs to arbitrate -- and
    because two groups that saw the same things then share a cache entry.
    """
    counts: "Counter[str]" = Counter()
    forms: dict[str, Counter] = {}
    for member in members:
        make = str(member.get("own_make") or member.get("make") or "").strip()
        model = str(member.get("own_model") or member.get("model") or "").strip()
        # The model often carries the make already, so the raw pair reads
        # "Holden Holden Commodore" -- and a reading that looks like a
        # mistake invites the model to correct things it was not asked to.
        if make and model.lower().startswith(make.lower()):
            make = ""
        line = " ".join(b for b in (make, model) if b).strip()
        if not line:
            continue
        key = _key(line)
        counts[key] += 1
        forms.setdefault(key, Counter())[(make, model)] += 1

    # Most-seen first, and carrying how often, because how often is the
    # evidence. See Reading.
    out = []
    for key, total in counts.most_common():
        (make, model), _ = forms[key].most_common(1)[0]
        out.append(Reading(make=make, model=model, count=total))
    return out


def _key(reading: str) -> str:
    """What two readings have to share to be the same reading.

    Measured on a real session: "Jaguar XJS" and "Jaguar XJ-S" each appeared
    twelve times, "MINI Cooper S" twenty-two and "Mini Cooper S" three,
    "Toyota Hilux" six and "Toyota HiLux" three. Every one of those pairs is
    one car written two ways, and treating them as a disagreement both cost a
    model call and let a group look divided when it was unanimous.

    Separators go entirely rather than becoming spaces, because both
    spellings occur: "XJ-S" has to match "XJS", and "Cooper-S" has to match
    "Cooper S". Dropping them handles both.

    Case and punctuation only. Anything cleverer -- stemming, fuzzy distance
    -- starts merging things that really are different, and "XJ6" against
    "XJS" is a different Jaguar.
    """
    return "".join(c for c in reading.lower() if c.isalnum())


def _observed(readings: "list[Reading]") -> str:
    return " | ".join(r.text for r in readings).lower()


def _acceptable_make(make: str | None, readings: list[str]) -> str | None:
    """A make is acceptable if it was read, or if a read nameplate implies it.

    The second half is the point: "Falcon FG" on its own never says Ford, but
    marques.py knows only Ford makes a Falcon, and that is knowledge with a
    source rather than a guess.
    """
    if not make:
        return None
    if make.lower() in _observed(readings):
        return make
    for reading in readings:
        implied = marques.correct_make(None, reading.text)
        if implied and implied.lower() == make.lower():
            return make
    return None


def _acceptable_model(model: str | None, readings: list[str]) -> str | None:
    """Keep the words of the model that were read, drop the ones that were not.

    Word by word rather than whole-string, so joining "Falcon" from one
    reading to "FG" from another is allowed -- that is exactly the tidying
    wanted -- while inventing "MkII" out of nothing is not.

    Trimming rather than discarding matters. Four frames read a Commodore as
    VC, VX and plain Commodore; the model answered "Commodore VE", which
    nobody saw. Throwing the whole answer away lost "Commodore" too, and
    every frame had seen that. So the invented series goes and the nameplate
    stays.
    """
    if not model:
        return None
    haystack = _observed(readings)
    kept = [w for w in model.replace("-", " ").split()
            if w.lower() in haystack]
    if not kept:
        return None
    tidied = " ".join(kept)

    # Give the spelling back. Splitting on the hyphen to check the words is
    # fine; rejoining with a space is not, and "X-Trail" came back as
    # "X Trail" -- which then failed to match the other X-Trails and split
    # one car into three groups. Where the answer is a reading that was
    # actually seen, use the way it was actually written.
    for reading in readings:
        for candidate in (reading.text, reading.model):
            if candidate and _key(candidate) == _key(tidied):
                return candidate
    return tidied


# A reading this much of the group agreed on is not a disagreement to
# arbitrate, it is the answer. Measured: twenty frames of one Jaguar read
# seventeen Jaguar and three Nissan, and asking a model to choose between
# them -- with the counts stripped out -- got the Nissan.
MAJORITY_SETTLES = 0.7


def canonical(readings, settings: Settings, *,
              client: httpx.Client | None = None,
              cache: dict | None = None) -> Canonical:
    """One canonical identity for a set of readings of the same vehicle."""
    if readings and isinstance(readings[0], str):     # tolerated for callers
        readings = readings_from(readings)
    if len(readings) < 2:
        # Nothing to reconcile. Not worth a model call, and asking one to
        # "tidy" a lone reading is how a reading gets embellished.
        return Canonical()

    # A clear majority needs no adjudicating, and adjudicating it is worse
    # than useless: it spends a call putting a well-supported answer at the
    # mercy of a model that cannot see how well supported it is.
    total = sum(r.count for r in readings)
    top = readings[0]
    if total and top.count / total >= MAJORITY_SETTLES:
        make = top.make or marques.correct_make(None, top.model)
        return Canonical(make=make or None, model=top.model or None)

    key = "\n".join(f"{r.count}x {r.text}" for r in readings)
    if cache is not None and key in cache:
        return cache[key]

    payload = {
        "model": settings.vlm_model,
        "prompt": PROMPT.format(readings="\n".join(
            f"- {r.text}   (read in {r.count} of {total} frames)"
            for r in readings)),
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0.0, "num_predict": 200},
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
        # Ollama being absent, busy or slow must never fail a scan. The group
        # keeps the name it voted for.
        return Canonical()
    finally:
        if owns_client:
            client.close()

    make = parsed.get("make")
    model = parsed.get("model")
    make = make.strip() if isinstance(make, str) else None
    model = model.strip() if isinstance(model, str) else None

    rejected: list[str] = []
    kept_make = _acceptable_make(make, readings)
    if make and not kept_make:
        rejected.append(make)
    kept_model = _acceptable_model(model, readings)
    if model and kept_model != model:
        rejected.append(model)

    # A model without its make is half an answer, and marques.py can often
    # supply the other half from the nameplate alone.
    if kept_model and not kept_make:
        kept_make = marques.correct_make(None, kept_model)

    # The make repeated into the model is the one thing worth stripping
    # without asking: it is the mistake the prompt already asks it to avoid.
    if kept_make and kept_model and kept_model.lower().startswith(kept_make.lower()):
        kept_model = kept_model[len(kept_make):].strip() or None

    result = Canonical(make=kept_make, model=kept_model, rejected=rejected)
    if cache is not None:
        cache[key] = result
    return result
