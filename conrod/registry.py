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
    """Every known car, keyed by its normalised plate.

    Read once at the start of a run rather than queried per detection: it is
    a few thousand short rows at most, and the analysis workers are already
    contending for this database.
    """
    known: dict[str, dict] = {}
    try:
        rows = conn.execute(
            f"SELECT plate, {', '.join(FIELDS)} FROM known_vehicles").fetchall()
    except sqlite3.OperationalError:      # table not created yet
        return known
    for row in rows:
        entry = {field: row[field] for field in FIELDS}
        entry["sponsors"] = _split(entry.get("sponsors"))
        known[normalise(row["plate"])] = entry
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

    existing = conn.execute(
        f"SELECT {', '.join(FIELDS)} FROM known_vehicles WHERE plate = ?",
        (plate,)).fetchone()
    if existing is None:
        conn.execute(
            f"""INSERT INTO known_vehicles (plate, {', '.join(FIELDS)}, updated_at)
                VALUES (?{', ?' * len(FIELDS)}, ?)""",
            (plate, *(fresh[f] for f in FIELDS), time.time()))
        return True

    merged = {f: (existing[f] or fresh[f]) for f in FIELDS}
    if all(merged[f] == existing[f] for f in FIELDS):
        return False
    conn.execute(
        f"""UPDATE known_vehicles
               SET {', '.join(f'{f} = ?' for f in FIELDS)}, updated_at = ?
             WHERE plate = ?""",
        (*(merged[f] for f in FIELDS), time.time(), plate))
    return True


def seed(conn: sqlite3.Connection, job_id: int | None = None) -> dict:
    """Build the registry out of what has already been identified.

    An album that has been through identification already holds the answers;
    this is what stops the registry starting empty on a machine that has been
    shooting for months. Runs the same merge as remember(), so it can be
    re-run and cannot blank anything.
    """
    where = "WHERE i.job_id = ?" if job_id is not None else ""
    args = (job_id,) if job_id is not None else ()
    rows = conn.execute(
        f"""SELECT d.plate, d.attributes, d.number
              FROM detections d JOIN images i ON i.id = d.image_id
              {where} {'AND' if where else 'WHERE'} d.plate IS NOT NULL
                AND d.plate != '' AND d.attributes IS NOT NULL
                AND d.attributes != ''""", args).fetchall()

    added = 0
    for row in rows:
        try:
            parsed = json.loads(row["attributes"])
        except (TypeError, ValueError):
            continue
        entry = _Reading(row["plate"], parsed, row["number"])
        if remember(conn, entry):
            added += 1
    return {"looked_at": len(rows), "written": added,
            "known": count(conn)}


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
        for field in FIELDS:
            setattr(self, field, parsed.get(field))
        if not getattr(self, "race_number", None):
            self.race_number = number
