"""SQLite job store.

A shoot is a *job*: a folder full of frames, each with zero or more detected
vehicles, each of which may have a number. The database is what the review UI
reads and writes, and what the XMP writer consumes at the end, so a run can be
interrupted and resumed without redoing work.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root          TEXT NOT NULL,
    label         TEXT,
    created_at    REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'scanning',
    settings_json TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    preview_path TEXT,
    width        INTEGER,
    height       INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    written_at   REAL,
    UNIQUE (job_id, path)
);

CREATE TABLE IF NOT EXISTS detections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    x1 REAL, y1 REAL, x2 REAL, y2 REAL,
    cls           TEXT,
    conf          REAL,
    crop_path     TEXT,
    number        TEXT,
    number_source TEXT,          -- 'ocr' | 'roundel' | 'vlm' | 'manual' | pairs
    number_conf   REAL,
    plate         TEXT,
    plate_state   TEXT,
    plate_conf    REAL,
    -- The full VehicleAnalysis as JSON: make, model, colour, team, sponsors,
    -- text. Kept as one blob because the fields are read and written together
    -- and the shape is still moving.
    attributes    TEXT,
    reviewed      INTEGER NOT NULL DEFAULT 0,
    rejected      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_images_job    ON images(job_id, status);
CREATE INDEX IF NOT EXISTS idx_det_image     ON detections(image_id);
CREATE INDEX IF NOT EXISTS idx_det_number    ON detections(number);
"""


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so they are applied against the existing table and the duplicate
# error swallowed — simpler and safer than a version table for a local tool.
_MIGRATIONS = [
    ("detections", "plate", "TEXT"),
    ("detections", "plate_state", "TEXT"),
    ("detections", "plate_conf", "REAL"),
    ("detections", "attributes", "TEXT"),
    ("detections", "signature", "TEXT"),
    ("detections", "group_key", "INTEGER"),
    ("detections", "group_size", "INTEGER"),
    ("detections", "group_agreement", "REAL"),
    ("detections", "colour_hex", "TEXT"),
    ("detections", "group_colour_hex", "TEXT"),
    # Which body shot it and which burst it belongs to. Two shooters at one
    # event interleave into a single folder, so neither filename nor
    # timestamp alone identifies a run of frames -- see bursts.py.
    ("images", "camera", "TEXT"),
    ("images", "burst_key", "INTEGER"),
    ("images", "taken_at", "REAL"),
    # How sharp the subject is, measured on the crop rather than the frame,
    # so a panning shot is not marked down for the blur that makes it good.
    ("detections", "sharpness", "REAL"),
    ("detections", "sharpness_verdict", "TEXT"),
    # Why a detection was cut before it was ever identified. Rejected with no
    # reason given is indistinguishable from rejected by a person, and the
    # difference matters when you are deciding whether to trust it.
    ("detections", "cull_reason", "TEXT"),
    # How many frame edges the subject runs off, and the rating that comes
    # out of combining that with focus. Sharpness stays a pure focus measure
    # -- a car cut in half at the edge is often perfectly sharp, and folding
    # the two together would make neither number mean anything.
    ("detections", "clipped", "INTEGER"),
    ("detections", "rating", "REAL"),
    ("detections", "rating_verdict", "TEXT"),
    # A star rating given by hand, which outranks the measured one
    # everywhere: sorting, and what Write XMP puts in the file. Kept in its
    # own column rather than written over `rating` so that re-culling an
    # album cannot quietly erase the photographer's own pass.
    ("detections", "stars", "INTEGER"),
    # A vehicle that is in the photograph but is not what the photograph is
    # of. Distinct from rejected: rejecting is a judgement about the frame,
    # this is a statement about which car in it is the subject.
    ("detections", "bystander", "INTEGER"),

    # Where the sharpness is, rather than how much of it there is. A held pan
    # has a sharp subject against a smeared background, and judged on the
    # whole picture it is the worst frame of the set instead of the best.
    ("detections", "panning", "INTEGER"),
    ("detections", "background", "REAL"),
    ("detections", "sharp_end", "TEXT"),

    # The cull was a close call and a person should see the frame. Culled all
    # the same -- the alternative is a shoot that quietly loses its panners.
    ("detections", "uncertain", "INTEGER"),

    # What the file already said before Conrod ever looked at it. A shoot
    # that has been through Lightroom once arrives with the photographer's
    # own opinion on it, and the cull has no business either ignoring that
    # or overwriting it.
    ("images", "rating_in_file", "INTEGER"),
    ("images", "label_in_file", "TEXT"),

    # What the crop looks like to the similarity model, as text. Kept beside
    # the detection so grouping can be re-run over a whole shoot without
    # opening a single crop again -- which is what makes "Group cars" cheap
    # enough to press after every correction.
    ("detections", "embedding", "TEXT"),

    # The frame's own sharpness, for frames with no vehicle in them at all.
    # The cull measures the car, which is right, and leaves a photograph with
    # no car in it unrated -- so a shoot's worth of frames the detector found
    # nothing in came back with no opinion of any kind, which is not the same
    # as "fine".
    # The star this photographer would probably give, learned from the ones
    # they have given already. Stored rather than computed on read because
    # sorting is SQL: the order-by has to be able to reach it.
    ("detections", "predicted_stars", "INTEGER"),

    ("images", "sharpness", "REAL"),
    ("images", "rating", "REAL"),
    ("images", "rating_verdict", "TEXT"),
]


_prepared: set[str] = set()
_prepare_lock = threading.Lock()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection, creating the schema at most once per process.

    This used to run executescript(SCHEMA) and the ALTER TABLE migrations on
    every single connection. Those are writes, so every HTTP request queued
    behind the analysis workers for the database write lock, and a scan being
    watched in the UI failed with "database is locked". Now the first
    connection prepares the file and the rest just open it.
    """
    target = str(path or DB_PATH)
    conn = sqlite3.connect(target, timeout=30, check_same_thread=False)
    # Autocommit. Python's default opens a transaction on the first write and
    # holds it until commit(), which meant the analysis workers -- batching a
    # commit every five detections, each detection a multi-second call to the
    # vision model -- sat on the single write lock for twenty to thirty
    # seconds at a stretch. Everything else waited, and on slower frames the
    # wait passed busy_timeout and the scan died with "database is locked".
    # Each statement is now its own transaction, which is what WAL is for.
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL lets the UI read while a scan writes. NORMAL is the matching
    # durability setting: a crash can lose the last commits, which for a
    # re-runnable scan is a fair trade for not fsyncing on every frame.
    conn.execute("PRAGMA busy_timeout=30000")

    if target not in _prepared:
        with _prepare_lock:
            if target not in _prepared:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(SCHEMA)
                _migrate(conn)
                _prepared.add(target)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in _MIGRATIONS:
        existing = {row["name"] for row in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- jobs -----------------------------------------------------------------

def create_job(conn: sqlite3.Connection, root: Path, label: str | None,
               settings: dict) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (root, label, created_at, settings_json) VALUES (?,?,?,?)",
        (str(root), label or root.name, time.time(), json.dumps(settings)),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_job_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    conn.commit()


def latest_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()


def list_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT j.*,
               (SELECT COUNT(*) FROM images i WHERE i.job_id = j.id) AS image_count,
               (SELECT COUNT(*) FROM detections d
                  JOIN images i2 ON i2.id = d.image_id
                 WHERE i2.job_id = j.id) AS detection_count,
               (SELECT COUNT(*) FROM images i3
                 WHERE i3.job_id = j.id AND i3.status != 'detected')
                 AS unfinished_count,
               -- Whether grouping has ever run on this album. Grouping is
               -- the last step of a full scan, so an album that was culled
               -- and stopped has none, and the review screen needs to say
               -- that rather than let every frame of one car look like a
               -- separate vehicle nobody grouped.
               (SELECT COUNT(*) FROM detections d2
                  JOIN images i4 ON i4.id = d2.image_id
                 WHERE i4.job_id = j.id AND d2.group_key IS NOT NULL)
                 AS grouped_count
          FROM jobs j ORDER BY j.id DESC
        """
    ).fetchall()


# --- images ---------------------------------------------------------------

def add_images(conn: sqlite3.Connection, job_id: int, paths: Sequence[Path]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO images (job_id, path) VALUES (?,?)",
        [(job_id, str(p)) for p in paths],
    )
    conn.commit()


def pending_images(conn: sqlite3.Connection, job_id: int,
                   status: str = "pending") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM images WHERE job_id=? AND status=? ORDER BY id",
        (job_id, status),
    ).fetchall()


def set_image_result(conn: sqlite3.Connection, image_id: int, *, status: str,
                     preview_path: str | None = None, width: int | None = None,
                     height: int | None = None, error: str | None = None) -> None:
    conn.execute(
        """UPDATE images
              SET status=?, preview_path=COALESCE(?, preview_path),
                  width=COALESCE(?, width), height=COALESCE(?, height), error=?
            WHERE id=?""",
        (status, preview_path, width, height, error, image_id),
    )


# --- detections -----------------------------------------------------------

def add_detection(conn: sqlite3.Connection, image_id: int, box: tuple,
                  cls: str, conf: float, crop_path: str) -> int:
    cur = conn.execute(
        """INSERT INTO detections (image_id, x1, y1, x2, y2, cls, conf, crop_path)
           VALUES (?,?,?,?,?,?,?,?)""",
        (image_id, *box, cls, conf, crop_path),
    )
    return int(cur.lastrowid)


def set_number(conn: sqlite3.Connection, det_id: int, number: str | None,
               source: str, confidence: float) -> None:
    conn.execute(
        "UPDATE detections SET number=?, number_source=?, number_conf=? WHERE id=?",
        (number, source, confidence, det_id),
    )


def set_analysis(conn: sqlite3.Connection, det_id: int, analysis,
                 colour_hex: str | None = None,
                 sharpness: float | None = None,
                 sharpness_verdict: str | None = None) -> None:
    """Store a completed VehicleAnalysis against a detection."""
    conn.execute(
        """UPDATE detections
              SET number=?, number_source=?, number_conf=?,
                  plate=?, plate_state=?, plate_conf=?, attributes=?,
                  colour_hex=COALESCE(?, colour_hex),
                  sharpness=COALESCE(?, sharpness),
                  sharpness_verdict=COALESCE(?, sharpness_verdict)
            WHERE id=?""",
        (analysis.race_number, analysis.number_source, analysis.number_conf,
         analysis.plate, analysis.plate_state, analysis.plate_conf,
         analysis.to_json(), colour_hex, sharpness, sharpness_verdict, det_id),
    )


def set_frame_origin(conn: sqlite3.Connection, job_id: int,
                     frames: "list") -> int:
    """Record which camera took each frame and which burst it belongs to.

    Written in one transaction after the folder is read, before any frame is
    analysed, so grouping and the review screen can both lean on it. Frames
    the scan has never heard of are ignored rather than inserted: this only
    annotates, it does not decide what is in the job.
    """
    # Matched on a normalised path, not the string. exiftool reports
    # SourceFile with forward slashes and the database holds what Windows
    # gave us, so comparing them directly matched nothing at all -- and
    # silently, because an UPDATE that hits no rows is not an error. Every
    # burst was computed correctly and then thrown away.
    known = {_path_key(row["path"]): row["id"] for row in conn.execute(
        "SELECT id, path FROM images WHERE job_id=?", (job_id,))}

    rows = []
    for frame in frames:
        image_id = known.get(_path_key(frame.path))
        if image_id is not None:
            rows.append((frame.camera, frame.burst, frame.taken, image_id))
    if not rows:
        return 0
    conn.executemany(
        "UPDATE images SET camera=?, burst_key=?, taken_at=? WHERE id=?", rows)
    conn.commit()
    return len(rows)


def set_existing_marks(conn: sqlite3.Connection, job_id: int,
                       marks: dict) -> int:
    """Record the rating and colour label each file already carried.

    ``marks`` is keyed by path, holding ``(rating, label)``. Matched on a
    normalised path for the same reason set_frame_origin is: exiftool hands
    back forward slashes and the database holds what Windows gave us.

    A rating of zero is not a rating. Every camera writes 0 into the field
    for "not rated", so treating it as a deliberate one star would put a
    floor under the whole shoot and quietly outvote the cull on every frame.
    """
    known = {_path_key(row["path"]): row["id"] for row in conn.execute(
        "SELECT id, path FROM images WHERE job_id=?", (job_id,))}

    rows = []
    for path, (rating, label) in marks.items():
        image_id = known.get(_path_key(path))
        if image_id is None:
            continue
        stars = rating if isinstance(rating, int) and 1 <= rating <= 5 else None
        rows.append((stars, (label or "").strip() or None, image_id))
    if not rows:
        return 0
    conn.executemany(
        "UPDATE images SET rating_in_file=?, label_in_file=? WHERE id=?", rows)
    conn.commit()
    return sum(1 for row in rows if row[0] is not None)


def _path_key(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def set_quality(conn: sqlite3.Connection, det_id: int, *,
                sharpness: float, sharpness_verdict: str,
                clipped: int, rating: float, rating_verdict: str,
                panning: bool = False, sharp_end: str = "even",
                background: float = -1.0, uncertain: bool = False) -> None:
    """Everything known about the picture, as opposed to what is in it."""
    conn.execute(
        """UPDATE detections
              SET sharpness=?, sharpness_verdict=?, clipped=?,
                  rating=?, rating_verdict=?, panning=?, background=?,
                  sharp_end=?, uncertain=?
            WHERE id=?""",
        (sharpness, sharpness_verdict, clipped, rating, rating_verdict,
         int(bool(panning)), background, sharp_end, int(bool(uncertain)),
         det_id))


def cull_detection(conn: sqlite3.Connection, det_id: int, reason: str,
                   uncertain: bool = False) -> None:
    """Cut a detection before it is identified, and say why.

    Rejected rather than deleted: the crop and its score stay, so the
    Rejected view can show what was cut and put anything back that should not
    have been.

    ``uncertain`` marks a cull that was a close call, so review can surface it
    rather than leaving it to be found by someone counting their frames.
    """
    conn.execute(
        "UPDATE detections SET rejected=1, cull_reason=?, uncertain=? WHERE id=?",
        (reason, int(bool(uncertain)), det_id))


def unread_detections(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.* FROM detections d
             JOIN images i ON i.id = d.image_id
            WHERE i.job_id=? AND d.number_source IS NULL
            ORDER BY d.id""",
        (job_id,),
    ).fetchall()


def set_embedding(conn: sqlite3.Connection, det_id: int, packed: str) -> None:
    """What the crop looks like to the similarity model."""
    conn.execute("UPDATE detections SET embedding=? WHERE id=?",
                 (packed, det_id))
    conn.commit()
