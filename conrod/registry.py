"""Cars this photographer has already met.

The same cars turn up at the same meets. A plate read on one Sunday is the
same car the next Sunday, and asking a vision model to work out what it is
all over again is paying twice for an answer already given -- and getting a
different one half the time, because the model disagrees with itself about
a fifth of the frames of a single burst.

So a plate is a key. Once a car has been identified with a plate on it, what
it turned out to be is remembered against that plate, and the next album
that sees the plate starts from that answer.

Two rules, and they are the whole design.

**It only fills in blanks.** What was read on the day always wins: if this
scan got a make off the badge, the registry does not touch it. A registry
that overruled the frame in front of it would be confidently wrong the first
time a car was resprayed or a plate moved to another shell, and it would be
wrong silently, everywhere, for as long as the row survived.

**The plate has to match exactly**, once punctuation and case are taken out.
Grouping is allowed to join 43111J to 73111J across a burst, because there
it has other evidence -- the same burst, the same crop, seconds apart -- and
the cost of being wrong is one pile. Here there is no other evidence and the
cost is a different car's identity written onto this one. So a misread plate
simply misses, which is the failure that does no damage.
"""

from __future__ import annotations

import collections
import csv
import io
import json
import re
import sqlite3
import time

# What a row holds. Deliberately the fields identification already produces,
# so seeding the registry from an album and writing an answer back into it
# both need no translation -- and a CSV someone edits in Excel has the same
# column names as the cards they are looking at.
FIELDS = ("make", "model", "colour", "body_type", "team", "sponsors",
          "race_number")

COLUMNS = ("plate",) + FIELDS


def normalise(plate: str | None) -> str:
    """The key a plate is stored under.

    Case and punctuation are noise: "39432J", "39432-J" and "39432 j" are one
    plate written three ways, and a registry that treats them as three cars
    is worse than no registry. Everything else is left alone -- in
    particular no character is corrected, because correcting one is how a
    lookup finds the wrong car.
    """
    if not plate:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(plate).upper())


def load(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every known car, keyed by its normalised plate and by its aliases.

    An alias is another reading of the same car's plate -- 43111J where
    eighteen frames said 73111J -- established by grouping from what the
    crops look like, inside one burst. So looking one up is not fuzzy
    matching: it is a plate this car was observed to be wearing, and the
    lookup is still exact.

    Read once at the start of a run rather than queried per detection: it is
    a few thousand short rows at most, and the analysis workers are already
    contending for this database.
    """
    known: dict[str, dict] = {}
    try:
        rows = conn.execute(
            f"SELECT plate, {', '.join(FIELDS)}, aliases "
            "FROM known_vehicles").fetchall()
    except sqlite3.OperationalError:      # table not created yet
        return known
    for row in rows:
        entry = {field: row[field] for field in FIELDS}
        entry["sponsors"] = _split(entry.get("sponsors"))
        known[normalise(row["plate"])] = entry
        for alias in _split(row["aliases"] if "aliases" in row.keys() else None):
            # The real plate wins if a misread of one car happens to be
            # another car's actual registration.
            known.setdefault(normalise(alias), entry)
    return known


def fill(analysis, known: dict[str, dict]) -> bool:
    """Fill this vehicle's blanks from what the plate is known to be.

    Returns whether anything was filled, which is only used for reporting.
    Never overwrites: a field the readers found on the day is the answer for
    that day, and this is a memory of another one.
    """
    if not known:
        return False
    entry = known.get(normalise(getattr(analysis, "plate", None)))
    if not entry:
        return False

    filled = False
    for field in FIELDS:
        remembered = entry.get(field)
        if not remembered:
            continue
        current = getattr(analysis, field, None)
        # A list field is blank when it is empty, not when it is None.
        if isinstance(remembered, list):
            if current:
                continue
        elif current:
            continue
        setattr(analysis, field, remembered)
        filled = True
    return filled


def remember(conn: sqlite3.Connection, analysis) -> bool:
    """Record what this plate turned out to be, as the cars go past.

    Called for every detection that has both a plate and something read off
    it, so the registry builds itself out of ordinary use rather than being
    something to sit down and fill in.

    The same not-overwriting rule applies in this direction too, so a later
    album cannot blank a field an earlier one filled: a frame where the
    model saw no team does not mean the car has no team, it means this
    photograph did not show one.
    """
    plate = normalise(getattr(analysis, "plate", None))
    if not plate:
        return False
    fresh = {field: _text(getattr(analysis, field, None)) for field in FIELDS}
    if not any(fresh.values()):
        return False

    # Other readings of this same car's plate, where the caller knows of
    # any. Only grouping does: it establishes them from what the crops look
    # like, so they are observations rather than guesses.
    aliases = {normalise(a) for a in _split(getattr(analysis, "aliases", None))}
    aliases.discard(plate)
    aliases.discard("")

    existing = conn.execute(
        f"SELECT {', '.join(FIELDS)}, aliases FROM known_vehicles "
        "WHERE plate = ?", (plate,)).fetchone()
    if existing is None:
        conn.execute(
            f"""INSERT INTO known_vehicles (plate, {', '.join(FIELDS)},
                                            aliases, updated_at)
                VALUES (?{', ?' * len(FIELDS)}, ?, ?)""",
            (plate, *(fresh[f] for f in FIELDS),
             ", ".join(sorted(aliases)) or None, time.time()))
        return True

    merged = {f: (existing[f] or fresh[f]) for f in FIELDS}
    both = aliases | {normalise(a) for a in _split(existing["aliases"])}
    both.discard(plate)
    both.discard("")
    kept = ", ".join(sorted(both)) or None
    if (all(merged[f] == existing[f] for f in FIELDS)
            and kept == (existing["aliases"] or None)):
        return False
    conn.execute(
        f"""UPDATE known_vehicles
               SET {', '.join(f'{f} = ?' for f in FIELDS)},
                   aliases = ?, updated_at = ?
             WHERE plate = ?""",
        (*(merged[f] for f in FIELDS), kept, time.time(), plate))
    return True


def seed(conn: sqlite3.Connection, job_id: int | None = None) -> dict:
    """Build the registry out of albums that have already been identified.

    Per *car*, not per frame, and that distinction is the whole of it. One
    real group of seventeen frames of a single XE Falcon wagon had the model
    read as Escort, Falcon, Cortina, Mustang, Granada, "Falcon XE Wagon",
    "XE Falcon Wagon" and "Audi 100" -- one car, eight answers. Seeding a
    frame at a time takes whichever of those happened to be attached to a
    frame that also carried a plate, and writes it down as fact.

    Grouping has already done this work and done it honestly: it publishes
    ``group_make`` and ``group_model`` where the frames agreed, and leaves
    them empty with the candidates in ``group_disputed`` where they did not.
    So this trusts that verdict and stores nothing where grouping declined
    to reach one. An empty column is a car whose model is still an open
    question, which is true; "Granada" would have been a lie with a
    provenance.

    The other spellings of the plate go in as aliases, so the misread that
    grouping already resolved is resolved here too rather than becoming a
    second car -- which is exactly what the first version of this did.

    Ungrouped detections are still seeded one at a time: an album nobody has
    pressed Group cars on has no verdict to trust, and one reading is better
    than nothing.
    """
    where = "WHERE i.job_id = ?" if job_id is not None else ""
    args = (job_id,) if job_id is not None else ()
    rows = conn.execute(
        f"""SELECT d.plate, d.attributes, d.number, d.group_key
              FROM detections d JOIN images i ON i.id = d.image_id
              {where} {'AND' if where else 'WHERE'} d.plate IS NOT NULL
                AND d.plate != '' AND d.attributes IS NOT NULL
                AND d.attributes != ''""", args).fetchall()

    cars: dict = {}
    loose: list = []
    for row in rows:
        try:
            parsed = json.loads(row["attributes"])
        except (TypeError, ValueError):
            continue
        if row["group_key"] is None:
            loose.append(_Reading(row["plate"], parsed, row["number"]))
        else:
            cars.setdefault(row["group_key"], []).append((row, parsed))

    added = 0
    for members in cars.values():
        reading = _agreed(members)
        if reading and remember(conn, reading):
            added += 1
    for reading in loose:
        if remember(conn, reading):
            added += 1
    return {"looked_at": len(rows), "cars": len(cars) + len(loose),
            "written": added, "known": count(conn)}


def _agreed(members: list) -> "_Reading | None":
    """One car's settled identity, out of every frame of it.

    Make and model come from grouping's own vote and are left blank where it
    could not settle. Everything else is a plain majority of the frames that
    offered a value -- which is safe for colour and body type in a way it is
    not for the model, because "blue" read eleven times and "navy" once is
    one car described twice, where Escort and Mustang are two different
    claims about what it is.
    """
    plates = collections.Counter()
    for row, _parsed in members:
        key = normalise(row["plate"])
        if key:
            plates[key] += 1
    if not plates:
        return None
    # The plate most of the frames read. The rest are readings of the same
    # car's plate, so they become the ways of finding it.
    (best, _count), = plates.most_common(1)
    # Only the ones that are plausibly the same plate misread -- one
    # confusable character, same length, which is grouping's own test.
    #
    # Grouping is looser than that across bursts, and on a real album it
    # over-merged: one car ended up holding EVL54L, 270SUS, 54L, EIL5AL and
    # MAY054, which are five different registrations. Inside a burst that
    # costs one pile and the crops are there to argue with. Written into the
    # registry it would be permanent, and it would hand one car's identity
    # to four others on every future album. So the registry keeps only the
    # readings it can see are readings of this plate.
    aliases = [plate for plate in plates
               if plate != best and _near_plate(plate, best)]

    first = members[0][1]
    reading = _Reading(best, {}, None)
    reading.aliases = aliases
    # Grouping's verdict, which is None when the frames disagreed.
    reading.make = first.get("group_make") or _majority(members, "make")
    reading.model = first.get("group_model")
    for field in ("colour", "body_type", "team", "race_number"):
        setattr(reading, field, _majority(members, field))
    reading.sponsors = _majority(members, "sponsors")
    return reading


def _near_plate(a: str, b: str) -> bool:
    """Whether these are one plate read two ways.

    Grouping's own test, reached lazily so that reading the registry does
    not drag in numpy and the similarity model behind it.
    """
    from .grouping import _near_plate as near

    return near(a, b)


def _majority(members: list, field: str):
    """The value most of this car's frames gave for one field."""
    votes = collections.Counter()
    for _row, parsed in members:
        value = parsed.get(field)
        if isinstance(value, list):
            for item in value:
                if item:
                    votes[str(item).strip()] += 1
        elif value:
            votes[str(value).strip()] += 1
    if not votes:
        return None
    if field == "sponsors":
        return [name for name, _n in votes.most_common()]
    return votes.most_common(1)[0][0]


def count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM known_vehicles").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def to_csv(conn: sqlite3.Connection) -> str:
    """The whole registry, for editing somewhere else and loading back."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in conn.execute(
            f"SELECT plate, {', '.join(FIELDS)} FROM known_vehicles "
            "ORDER BY plate"):
        writer.writerow([row[column] or "" for column in COLUMNS])
    return out.getvalue()


def from_csv(conn: sqlite3.Connection, text: str) -> dict:
    """Load a CSV back in. What is in the file wins.

    The opposite rule to remember(), and deliberately: this is somebody
    sitting down and correcting the registry by hand, which is the one case
    where the new value should replace the old one. A blank cell is left
    alone rather than treated as an instruction to erase, so a file with only
    the plate and model columns filled in is a usable edit.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "plate" not in [
            (name or "").strip().lower() for name in reader.fieldnames]:
        raise ValueError("that file has no 'plate' column")

    written = skipped = 0
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip()
               for k, v in raw.items() if k}
        plate = normalise(row.get("plate"))
        if not plate:
            skipped += 1
            continue
        given = {f: row.get(f) or None for f in FIELDS}
        existing = conn.execute(
            f"SELECT {', '.join(FIELDS)} FROM known_vehicles WHERE plate = ?",
            (plate,)).fetchone()
        if existing is None:
            conn.execute(
                f"""INSERT INTO known_vehicles (plate, {', '.join(FIELDS)},
                                                updated_at)
                    VALUES (?{', ?' * len(FIELDS)}, ?)""",
                (plate, *(given[f] for f in FIELDS), time.time()))
        else:
            merged = {f: (given[f] if given[f] is not None else existing[f])
                      for f in FIELDS}
            conn.execute(
                f"""UPDATE known_vehicles
                       SET {', '.join(f'{f} = ?' for f in FIELDS)},
                           updated_at = ?
                     WHERE plate = ?""",
                (*(merged[f] for f in FIELDS), time.time(), plate))
        written += 1
    return {"written": written, "skipped": skipped, "known": count(conn)}


def forget(conn: sqlite3.Connection, plate: str) -> bool:
    cur = conn.execute("DELETE FROM known_vehicles WHERE plate = ?",
                       (normalise(plate),))
    return bool(cur.rowcount)


def _split(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _text(value) -> str | None:
    """One field, as it goes into a text column."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        joined = ", ".join(str(v).strip() for v in value if str(v).strip())
        return joined or None
    text = str(value).strip()
    return text or None


class _Reading:
    """What seed() hands to remember(), shaped like a VehicleAnalysis.

    A plain namespace rather than the real class: seeding reads stored JSON
    and has no need for any of the behaviour, and building a VehicleAnalysis
    would drag the analysis module in for the sake of seven attributes.
    """

    def __init__(self, plate, parsed: dict, number=None):
        self.plate = plate
        self.aliases: list = []
        for field in FIELDS:
            setattr(self, field, parsed.get(field))
        if not getattr(self, "race_number", None):
            self.race_number = number
