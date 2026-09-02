"""Local application server.

Serves the whole desktop app: the first-run wizard, settings, the scan runner
and the review screen. Everything is on localhost and reads the same SQLite
database the pipeline writes, so review can start while a run is still going.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import keywords as keywords_mod
from . import pipeline, setup_check, store
from .analyze import VehicleAnalysis
from .config import DATA_ROOT, DEFAULTS, IMAGE_SUFFIXES, Settings
from .mapping import NumberMap

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Conrod")

_state: dict[str, Any] = {
    "settings": Settings.load(),
    "number_map": NumberMap(),
    "map_path": None,
}
_write_lock = threading.Lock()

# One scan at a time; the GPU and the detector are both single-tenant here.
_run: dict[str, Any] = {
    "active": False, "job_id": None, "stage": "", "done": 0, "total": 0,
    "message": "", "error": None, "started": 0.0, "stop": False,
    "paused": False, "current": None, "preview": None, "frame_token": 0,
}
_run_lock = threading.Lock()

# Set means "keep going". The detect loop waits on this between frames, so a
# pause takes effect within one frame and leaves the analysis pool to drain
# what it already has rather than abandoning work in flight.
_resume_gate = threading.Event()
_resume_gate.set()

# Recently detected frames, keyed by detection id, so a vehicle result arriving
# from the analysis pool can be matched back to the frame it came from.
_frames: dict[int, dict] = {}

# What the scan has been doing, for the panel in the app. exiftool, the
# detector and the vision model all write to stdout, which a windowed build
# does not have, so without this the long stages are a black box.
_journal: deque = deque(maxlen=400)
_journal_lock = threading.Lock()


def note(text: str, level: str = "info") -> None:
    if not text:
        return
    with _journal_lock:
        if _journal and _journal[-1]["text"] == text:
            _journal[-1]["repeat"] = _journal[-1].get("repeat", 1) + 1
            return
        _journal.append({"at": time.time(), "level": level, "text": text})


def _show(record: dict) -> None:
    """Put a frame on the live scan view."""
    if _run.get("preview") != record.get("preview"):
        _run["preview"] = record.get("preview")
        _run["frame_token"] = _run.get("frame_token", 0) + 1
    _run["current"] = {
        "name": record.get("name"), "boxes": record.get("boxes", []),
        "phase": record.get("phase", "SCANNING"), "log": record.get("log", []),
    }

_fix: dict[str, Any] = {"active": False, "name": "", "status": "", "percent": 0.0}


def configure(settings: Settings | None = None,
              number_map: NumberMap | None = None) -> None:
    if settings is not None:
        _state["settings"] = settings
    if number_map is not None:
        _state["number_map"] = number_map


def current_settings() -> Settings:
    return _state["settings"]


# --- pages ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


class _NoCacheStatic(StaticFiles):
    """Serve the UI uncached.

    This is a local app whose CSS and JS get edited in place; a cached
    stylesheet silently serving the previous version costs more than re-reading
    a few KB from disk over localhost.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", _NoCacheStatic(directory=WEB_DIR), name="static")


# --- setup / wizard -------------------------------------------------------

@app.get("/api/setup")
def setup_status() -> dict:
    env = setup_check.inspect(_state["settings"])
    return {**env.to_dict(), "fix": _fix}


class FixRequest(BaseModel):
    name: str


@app.post("/api/setup/fix")
def setup_fix(body: FixRequest) -> dict:
    if _fix["active"]:
        raise HTTPException(409, "a setup step is already running")

    def progress(event: dict) -> None:
        _fix["status"] = event.get("status", "")
        if "percent" in event:
            _fix["percent"] = event["percent"]

    def worker() -> None:
        _fix.update({"active": True, "name": body.name, "status": "starting",
                     "percent": 0.0})
        try:
            setup_check.apply_fix(body.name, _state["settings"], progress)
        except Exception as exc:
            _fix["status"] = f"failed: {exc}"
        finally:
            _fix["active"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


# --- settings -------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    return {
        "settings": _state["settings"].to_dict(),
        "defaults": DEFAULTS.to_dict(),
        "map_path": _state["map_path"],
        "map_size": len(_state["number_map"]),
    }


class SettingsUpdate(BaseModel):
    settings: dict = {}
    map_path: str | None = None


@app.post("/api/settings")
def update_settings(body: SettingsUpdate) -> dict:
    settings: Settings = _state["settings"]
    settings.apply(body.settings)
    settings.save()

    if body.map_path is not None:
        path = body.map_path.strip()
        if not path:
            _state["number_map"], _state["map_path"] = NumberMap(), None
        else:
            try:
                _state["number_map"] = NumberMap.load(Path(path))
                _state["map_path"] = path
            except Exception as exc:
                raise HTTPException(400, f"could not read that CSV: {exc}")

    return get_settings()


# --- browsing for a folder ------------------------------------------------

@app.post("/api/pick-folder")
def pick_folder() -> dict:
    """Show the real Explorer folder dialog.

    The window is a browser window, so the page cannot open this itself. The
    in-page folder list stays as a fallback for when the dialog cannot run.
    """
    from . import nativeui

    try:
        chosen = nativeui.pick_folder()
    except OSError as exc:
        raise HTTPException(500, f"folder dialog unavailable: {exc}")
    return {"path": chosen or ""}


@app.get("/api/browse")
def browse(path: str | None = None) -> dict:
    """A minimal folder browser, for when no native dialog is available."""
    if not path:
        roots = [f"{d}:\\" for d in "CDEFGH" if Path(f"{d}:\\").exists()]
        return {"path": "", "parent": None, "dirs": roots, "counts": {}}

    target = Path(path)
    if not target.is_dir():
        raise HTTPException(400, "not a folder")
    try:
        entries = sorted(
            [p for p in target.iterdir() if p.is_dir()
             and not p.name.startswith(".")],
            key=lambda p: p.name.lower(),
        )
    except PermissionError:
        raise HTTPException(403, "no permission to read that folder")

    images = sum(1 for p in target.iterdir()
                 if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "dirs": [str(p) for p in entries],
        "images": images,
    }


@app.get("/api/browse/count")
def browse_count(path: str, recursive: bool = True) -> dict:
    """How many frames a scan of this folder would cover."""
    target = Path(path)
    if not target.is_dir():
        raise HTTPException(400, "not a folder")
    files = pipeline.scan(target, recursive)
    return {"path": str(target), "frames": len(files),
            "sample": [f.name for f in files[:5]]}


class EntryList(BaseModel):
    name: str
    text: str


@app.post("/api/entries")
def upload_entries(body: EntryList) -> dict:
    """Take an entry-list CSV chosen in the scan wizard.

    The text comes in the request rather than as a multipart upload so the
    app needs no extra dependency for one small file. It is saved under the
    data root so the same list is still loaded next time.
    """
    name = Path(body.name or "entries.csv").name
    if not name.lower().endswith(".csv"):
        name += ".csv"
    target = DATA_ROOT / "entries" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.text, encoding="utf-8")

    try:
        mapping = NumberMap.load(target)
    except (ValueError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))

    _state["number_map"], _state["map_path"] = mapping, str(target)
    settings: Settings = _state["settings"]
    settings.extra["map_path"] = str(target)
    settings.save()
    return {"ok": True, "entries": len(mapping), "path": str(target),
            "sample": list(mapping.rows)[:6]}


# --- running a scan -------------------------------------------------------

class ScanRequest(BaseModel):
    path: str
    label: str | None = None
    recursive: bool = True
    resume_job: int | None = None


@app.post("/api/scan")
def start_scan(body: ScanRequest) -> dict:
    with _run_lock:
        if _run["active"]:
            raise HTTPException(409, "a scan is already running")
        root = Path(body.path)
        if not root.is_dir():
            raise HTTPException(400, "not a folder")
        _frames.clear()
        _resume_gate.set()
        _run.update({"active": True, "job_id": body.resume_job,
                     "stage": "starting", "done": 0, "total": 0, "message": "",
                     "error": None, "started": time.time(), "stop": False,
                     "paused": False})

    def progress(event: dict) -> None:
        stage = event.get("stage", _run["stage"])
        if stage != "vehicle":
            _run["done"] = event.get("done", _run["done"])
            _run["total"] = event.get("total", _run["total"])
        if event.get("message"):
            _run["message"] = event["message"]
            if stage not in ("frame", "vehicle"):
                note(f"{stage}: {event['message']}",
                     "warn" if stage == "warn" else "info")
        _run["stage"] = stage

        # The live view follows the frame whose vehicles are being *read*, not
        # the one being detected. Detection runs several frames ahead of the
        # vision model, so showing the detection frontier would mean boxes that
        # never get their labels before the image changes underneath them.
        if stage == "frame":
            frame = event.get("frame") or {}
            if frame.get("phase") == "scanning":
                return
            boxes = frame.get("boxes") or []
            record = {
                "name": frame.get("name"), "preview": frame.get("preview"),
                "boxes": boxes, "phase": "VEHICLES FOUND" if boxes else "NO VEHICLE",
                "log": [f"{len(boxes)} vehicle{'' if len(boxes) == 1 else 's'} detected"
                        if boxes else "no vehicle detected"],
            }
            for box in boxes:
                _frames[box["id"]] = record
            # Trim the ring; a long scan would otherwise hold every frame.
            while len(_frames) > 400:
                _frames.pop(next(iter(_frames)))
            if not boxes:
                _show(record)

        elif stage == "vehicle":
            vehicle = event.get("vehicle") or {}
            record = _frames.get(vehicle.get("id"))
            if not record:
                return
            for box in record["boxes"]:
                if box.get("id") == vehicle.get("id"):
                    box["number"] = vehicle.get("number")
                    box["title"] = vehicle.get("title")
                    box["read_conf"] = vehicle.get("conf")

            log = record["log"]
            if vehicle.get("number"):
                log.append(f"number identified: {vehicle['number']}")
                who = _state["number_map"].describe(vehicle["number"])
                if who:
                    log.append(f"entry list: {who}")
            if vehicle.get("plate"):
                log.append(f"plate: {vehicle['plate']}")
            if vehicle.get("title"):
                log.append(f"identified: {vehicle['title']}")
            if vehicle.get("team"):
                log.append(f"team: {vehicle['team']}")
            record["log"] = log[-8:]
            record["phase"] = "READING"
            _show(record)

    def worker() -> None:
        try:
            summary = pipeline.run(
                root, _state["settings"], label=body.label,
                recursive=body.recursive, on_progress=progress,
                should_stop=lambda: _run["stop"],
                wait_if_paused=_resume_gate.wait,
                resume_job=body.resume_job,
            )
            _run["job_id"] = summary.job_id
            _run["stage"] = "done"
            _run["message"] = (f"{summary.images} frames, "
                               f"{summary.detections} vehicles, "
                               f"{summary.identified} identified")
        except Exception as exc:
            _run["error"] = f"{type(exc).__name__}: {exc}"
            _run["stage"] = "error"
            note(_run["error"], "error")
        finally:
            _run["active"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


@app.get("/api/scan")
def scan_status() -> dict:
    elapsed = time.time() - _run["started"] if _run["started"] else 0
    out = dict(_run)
    out["elapsed"] = round(elapsed, 1)
    if _run["active"] and _run["done"] and _run["total"]:
        rate = _run["done"] / max(elapsed, 0.001)
        out["eta"] = round((_run["total"] - _run["done"]) / max(rate, 0.001))
    return out


@app.post("/api/scan/stop")
def stop_scan() -> dict:
    _run["stop"] = True
    _resume_gate.set()          # a paused scan must be able to stop
    return {"ok": True}


@app.post("/api/scan/pause")
def pause_scan() -> dict:
    """Hold the scan between frames. Whatever is mid-analysis still finishes."""
    if not _run["active"]:
        raise HTTPException(409, "no scan is running")
    _resume_gate.clear()
    _run["paused"] = True
    return {"ok": True, "paused": True}


@app.post("/api/scan/resume")
def resume_scan() -> dict:
    _resume_gate.set()
    _run["paused"] = False
    return {"ok": True, "paused": False}


@app.get("/api/log")
def read_log(after: float = 0.0) -> dict:
    """What the scan has been doing. Everything the console would have shown."""
    with _journal_lock:
        lines = [e for e in _journal if e["at"] > after]
    return {"lines": lines, "at": lines[-1]["at"] if lines else after}


@app.get("/api/scan/frame")
def scan_frame():
    """The preview of the frame currently being scanned.

    Served by token rather than path so the browser refetches when the frame
    changes but caches within a frame, and so no arbitrary path can be read.
    """
    preview = _run.get("preview")
    if not preview:
        raise HTTPException(404, "no frame in flight")
    path = Path(preview)
    if not path.exists():
        raise HTTPException(404, "preview is gone")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


# --- jobs -----------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with store.session() as conn:
        return [dict(r) for r in store.list_jobs(conn)]


@app.get("/api/jobs/{job_id}/summary")
def job_summary(job_id: int) -> dict:
    settings: Settings = _state["settings"]
    with store.session() as conn:
        conn.create_function("_needs_review", 4, _needs_review)
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "no such job")

        counts = conn.execute(
            """SELECT
                 COUNT(*)                                            AS detections,
                 SUM(CASE WHEN d.number IS NOT NULL THEN 1 END)      AS numbered,
                 SUM(CASE WHEN d.plate IS NOT NULL THEN 1 END)       AS plated,
                 SUM(CASE WHEN d.rejected = 1 THEN 1 END)            AS rejected,
                 SUM(CASE WHEN d.reviewed = 1 THEN 1 END)            AS reviewed,
                 SUM(CASE WHEN _needs_review(d.number_conf, d.reviewed,
                                             d.rejected, :thresh) = 1
                          THEN 1 END)                                AS to_review
               FROM detections d JOIN images i ON i.id = d.image_id
              WHERE i.job_id = :job_id""",
            {"job_id": job_id, "thresh": settings.ocr_accept_confidence},
        ).fetchone()

        images = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN written_at IS NOT NULL THEN 1 END) AS written,
                      SUM(CASE WHEN status='error' THEN 1 END)         AS errors
                 FROM images WHERE job_id = ?""",
            (job_id,),
        ).fetchone()

        numbers = conn.execute(
            """SELECT d.number AS number, COUNT(*) AS frames
                 FROM detections d JOIN images i ON i.id = d.image_id
                WHERE i.job_id = ? AND d.number IS NOT NULL AND d.rejected = 0
                GROUP BY d.number ORDER BY frames DESC""",
            (job_id,),
        ).fetchall()

        number_map: NumberMap = _state["number_map"]
        return {
            "job": dict(job),
            "counts": {k: (v or 0) for k, v in dict(counts).items()},
            "images": {k: (v or 0) for k, v in dict(images).items()},
            "numbers": [{**dict(n), "who": number_map.describe(n["number"])}
                        for n in numbers],
            "map_size": len(number_map),
        }


# --- detections -----------------------------------------------------------

DETECTION_QUERY = """
SELECT d.id, d.number, d.number_source, d.number_conf, d.conf, d.cls,
       d.plate, d.plate_state, d.plate_conf, d.attributes,
       d.reviewed, d.rejected, d.group_key, d.group_size, d.group_agreement,
       i.path AS image_path, i.id AS image_id
  FROM detections d
  JOIN images i ON i.id = d.image_id
 WHERE i.job_id = :job_id
"""


@app.get("/api/jobs/{job_id}/detections")
def detections(
    job_id: int,
    view: str = Query("review", pattern="^(review|all|number|plate|rejected)$"),
    number: str | None = None,
    search: str | None = None,
    limit: int = 120,
    offset: int = 0,
) -> dict:
    settings: Settings = _state["settings"]
    clauses: list[str] = []
    params: dict[str, Any] = {"job_id": job_id, "limit": limit, "offset": offset}

    if view == "review":
        clauses.append(
            "_needs_review(d.number_conf, d.reviewed, d.rejected, :thresh) = 1"
        )
        params["thresh"] = settings.ocr_accept_confidence
    elif view == "number":
        if not number:
            raise HTTPException(400, "view=number needs a number")
        clauses.append("d.number = :number AND d.rejected = 0")
        params["number"] = number
    elif view == "plate":
        clauses.append("d.plate IS NOT NULL AND d.rejected = 0")
    elif view == "rejected":
        clauses.append("d.rejected = 1")

    if search:
        clauses.append(
            "(IFNULL(d.attributes,'') LIKE :q OR IFNULL(d.number,'') LIKE :q "
            "OR IFNULL(d.plate,'') LIKE :q OR i.path LIKE :q)"
        )
        params["q"] = f"%{search}%"

    sql = DETECTION_QUERY
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY d.number_conf ASC, d.id ASC LIMIT :limit OFFSET :offset"

    with store.session() as conn:
        conn.create_function("_needs_review", 4, _needs_review)
        rows = conn.execute(sql, params).fetchall()
        count_sql = ("SELECT COUNT(*) FROM detections d "
                     "JOIN images i ON i.id=d.image_id WHERE i.job_id = :job_id")
        if clauses:
            count_sql += " AND " + " AND ".join(clauses)
        total = conn.execute(count_sql, params).fetchone()[0]

    number_map: NumberMap = _state["number_map"]
    items = []
    for row in rows:
        item = dict(row)
        raw_attributes = item.pop("attributes", None)
        analysis = VehicleAnalysis.from_json(raw_attributes)
        item["filename"] = Path(item["image_path"]).name
        item["who"] = number_map.describe(item["number"]) if item["number"] else ""
        item["crop_url"] = f"/api/crop/{item['id']}"
        item["frame_url"] = f"/api/frame/{item['image_id']}"
        item["attributes"] = analysis.to_dict() if hasattr(analysis, "to_dict") else {}
        # Read straight from the stored JSON: the disputed list is written by
        # grouping and is not a field of VehicleAnalysis, so round-tripping
        # through it would drop the list silently.
        try:
            item["disputed"] = (json.loads(raw_attributes or "{}")
                                .get("group_disputed") or [])
        except (TypeError, ValueError):
            item["disputed"] = []
        item["title"] = analysis.title
        item["keywords"] = keywords_mod.for_vehicle(analysis, settings, number_map)
        items.append(item)
    return {"total": total, "items": items}


def _needs_review(number_conf, reviewed, rejected, threshold) -> int:
    """A detection a human should still look at.

    Registered as a SQL function so the summary count and the review grid
    cannot drift apart — they were inconsistent when this logic was duplicated.
    """
    if reviewed or rejected:
        return 0
    return 1 if (number_conf is None or number_conf < (threshold or 0.8)) else 0


class DetectionUpdate(BaseModel):
    number: str | None = None
    plate: str | None = None
    attributes: dict | None = None
    rejected: bool | None = None
    reviewed: bool = True


@app.post("/api/jobs/{job_id}/regroup")
def regroup(job_id: int) -> dict:
    """Re-run grouping after corrections in review.

    Works from the stored crops, so it does not re-read a single photo.
    """
    from . import grouping

    with store.session() as conn:
        return grouping.consolidate(conn, job_id, _state["settings"])


@app.post("/api/detections/{det_id}")
def update_detection(det_id: int, body: DetectionUpdate) -> dict:
    settings: Settings = _state["settings"]
    with store.session() as conn:
        row = conn.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such detection")

        analysis = VehicleAnalysis.from_json(row["attributes"])
        number, source, confidence = row["number"], row["number_source"], row["number_conf"]
        plate = row["plate"]

        if body.number is not None:
            cleaned = "".join(ch for ch in body.number if ch.isdigit())
            number = cleaned or None
            source, confidence = "manual", 1.0   # a human is ground truth
            analysis.race_number = number
            analysis.number_source, analysis.number_conf = source, confidence

        if body.plate is not None:
            cleaned = "".join(ch for ch in body.plate if ch.isalnum()).upper()
            plate = cleaned or None
            analysis.plate = plate
            analysis.plate_conf = 1.0 if plate else 0.0

        if body.attributes:
            for key in ("make", "model", "colour", "body_type", "team"):
                if key in body.attributes:
                    value = (body.attributes[key] or "").strip() or None
                    setattr(analysis, key, value)
                    if key == "team" and value:
                        # A human typed it, so it no longer needs corroborating.
                        analysis.team_corroborated = True

        rejected = row["rejected"] if body.rejected is None else int(body.rejected)
        conn.execute(
            """UPDATE detections
                  SET number=?, number_source=?, number_conf=?,
                      plate=?, attributes=?, rejected=?, reviewed=?
                WHERE id=?""",
            (number, source, confidence, plate, analysis.to_json(),
             rejected, int(body.reviewed), det_id),
        )

    number_map: NumberMap = _state["number_map"]
    return {
        "ok": True, "id": det_id, "number": number, "plate": plate,
        "who": number_map.describe(number) if number else "",
        "title": analysis.title,
        "keywords": keywords_mod.for_vehicle(analysis, settings, number_map),
    }


class BulkUpdate(BaseModel):
    ids: list[int]
    number: str | None = None
    rejected: bool | None = None


@app.post("/api/detections/bulk")
def bulk_update(body: BulkUpdate) -> dict:
    if not body.ids:
        return {"ok": True, "updated": 0}
    with store.session() as conn:
        marks = ",".join("?" * len(body.ids))
        if body.number is not None:
            cleaned = "".join(ch for ch in body.number if ch.isdigit()) or None
            for row in conn.execute(
                    f"SELECT id, attributes FROM detections WHERE id IN ({marks})",
                    body.ids).fetchall():
                analysis = VehicleAnalysis.from_json(row["attributes"])
                analysis.race_number = cleaned
                analysis.number_source, analysis.number_conf = "manual", 1.0
                conn.execute(
                    """UPDATE detections
                          SET number=?, number_source='manual', number_conf=1.0,
                              reviewed=1, attributes=?
                        WHERE id=?""",
                    (cleaned, analysis.to_json(), row["id"]),
                )
        if body.rejected is not None:
            conn.execute(
                f"UPDATE detections SET rejected=?, reviewed=1 WHERE id IN ({marks})",
                [int(body.rejected), *body.ids],
            )
    return {"ok": True, "updated": len(body.ids)}


# --- images ---------------------------------------------------------------

@app.get("/api/jobs/{job_id}/cover")
def job_cover(job_id: int) -> FileResponse:
    """A representative crop, used as the job card's thumbnail."""
    with store.session() as conn:
        row = conn.execute(
            """SELECT d.crop_path FROM detections d
                 JOIN images i ON i.id = d.image_id
                WHERE i.job_id = ? AND d.crop_path IS NOT NULL AND d.rejected = 0
                ORDER BY d.conf DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
    if not row or not Path(row["crop_path"]).exists():
        raise HTTPException(404, "no cover")
    return FileResponse(Path(row["crop_path"]), media_type="image/jpeg")


# Crops are written at up to 2048px because the readers need that
# resolution. The review grid shows them in a 268px card, so serving the
# originals meant ~14 MB and sixty full-size decodes for one screen of
# results. Thumbnails are generated once and cached beside the crop.
THUMB_WIDTHS = (420, 900)


def _thumbnail(path: Path, width: int) -> Path:
    thumb = path.with_suffix(f".t{width}.jpg")
    if thumb.exists() and thumb.stat().st_mtime >= path.stat().st_mtime:
        return thumb
    try:
        with Image.open(path) as img:
            if img.width <= width:
                return path
            img = img.convert("RGB")
            height = round(img.height * width / img.width)
            img.resize((width, height), Image.LANCZOS).save(
                thumb, "JPEG", quality=82, optimize=True)
        return thumb
    except Exception:
        return path


@app.get("/api/crop/{det_id}")
def crop(det_id: int, w: int | None = None) -> FileResponse:
    with store.session() as conn:
        row = conn.execute("SELECT crop_path FROM detections WHERE id=?",
                           (det_id,)).fetchone()
    if not row or not row["crop_path"]:
        raise HTTPException(404, "no crop")
    path = Path(row["crop_path"])
    if not path.exists():
        raise HTTPException(404, "crop file is gone")
    if w:
        width = min(THUMB_WIDTHS, key=lambda c: abs(c - w))
        path = _thumbnail(path, width)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/frame/{image_id}")
def frame(image_id: int) -> FileResponse:
    """The whole frame, for when a crop alone is not enough to judge."""
    with store.session() as conn:
        row = conn.execute("SELECT preview_path, path FROM images WHERE id=?",
                           (image_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such image")
    path = Path(row["preview_path"] or row["path"])
    if not path.exists() or path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise HTTPException(404, "no viewable preview")
    return FileResponse(path, media_type="image/jpeg")


# --- writing --------------------------------------------------------------

class WriteRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/jobs/{job_id}/write")
def write(job_id: int, body: WriteRequest) -> JSONResponse:
    # Writing touches the user's files, so never let two runs overlap.
    if not _write_lock.acquire(blocking=False):
        raise HTTPException(409, "a write is already running")
    try:
        result = pipeline.write_job(job_id, _state["settings"],
                                    _state["number_map"], dry_run=body.dry_run)
        return JSONResponse(result)
    finally:
        _write_lock.release()
