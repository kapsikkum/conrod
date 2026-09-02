"""SQLite job store.

A shoot is a *job*: a folder full of frames, each with zero or more detected
vehicles, each of which may have a number. The database is what the review UI
reads and writes, and what the XMP writer consumes at the end, so a run can be
interrupted and resumed without redoing work.
"""

from __future__ import annotations

import json
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
                 WHERE i2.job_id = j.id) AS detection_count
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


def set_analysis(conn: sqlite3.Connection, det_id: int, analysis) -> None:
    """Store a completed VehicleAnalysis against a detection."""
    conn.execute(
        """UPDATE detections
              SET number=?, number_source=?, number_conf=?,
                  plate=?, plate_state=?, plate_conf=?, attributes=?
            WHERE id=?""",
        (analysis.race_number, analysis.number_source, analysis.number_conf,
         analysis.plate, analysis.plate_state, analysis.plate_conf,
         analysis.to_json(), det_id),
    )


def unread_detections(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.* FROM detections d
             JOIN images i ON i.id = d.image_id
            WHERE i.job_id=? AND d.number_source IS NULL
            ORDER BY d.id""",
        (job_id,),
    ).fetchall()
