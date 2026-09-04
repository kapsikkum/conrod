"""Local application server.

Serves the whole desktop app: the first-run wizard, settings, the scan runner
and the review screen. Everything is on localhost and reads the same SQLite
database the pipeline writes, so review can start while a run is still going.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

from . import keywords as keywords_mod
from . import pipeline, setup_check, store, watch
from . import sharpness as sharpness_mod
from .analyze import VehicleAnalysis
from .config import (CACHE_DIR, DATA_ROOT, DEFAULTS, IMAGE_SUFFIXES,
                     Settings)
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


# The frames on screen right now, newest last, keyed by preview path. The
# analysis pool works several crops at once and they belong to different
# photographs, so a single "current frame" was whichever worker happened to
# report last -- the view flickered between unrelated cars and settled on
# none of them. Showing what is actually in flight is both calmer and more
# honest about what the machine is doing.
_live: "OrderedDict[str, dict]" = OrderedDict()
_live_lock = threading.Lock()

# How many frames the view holds. More than the pool has in flight is just
# stale photographs kept on screen to look busy.
LIVE_FRAMES = 6


def _show(record: dict) -> None:
    """Put a frame on the live scan view, or update it if already there."""
    preview = record.get("preview")
    with _live_lock:
        existing = _live.get(preview)
        if existing is None:
            _run["frame_token"] = _run.get("frame_token", 0) + 1
            token = _run["frame_token"]
        else:
            token = existing["token"]
            _live.move_to_end(preview)

        _live[preview] = {
            "token": token, "preview": preview,
            "name": record.get("name"), "boxes": record.get("boxes", []),
            "phase": record.get("phase", "SCANNING"),
            "log": record.get("log", []), "at": time.time(),
        }
        while len(_live) > LIVE_FRAMES:
            _live.popitem(last=False)

        # Kept for anything still reading the old single-frame shape.
        newest = next(reversed(_live.values()))
        _run["preview"] = newest["preview"]
        _run["current"] = newest


def _live_frames() -> list[dict]:
    """Newest first, which is the order they are drawn in."""
    with _live_lock:
        return [dict(entry) for entry in reversed(_live.values())]


def _preview_for(token: int) -> str | None:
    with _live_lock:
        for entry in _live.values():
            if entry["token"] == token:
                return entry["preview"]
    return None


def _clear_live() -> None:
    with _live_lock:
        _live.clear()

_fix: dict[str, Any] = {"active": False, "name": "", "status": "", "percent": 0.0}


def configure(settings: Settings | None = None,
              number_map: NumberMap | None = None) -> None:
    if settings is not None:
        _state["settings"] = settings
        # Where the loaded entry list came from. main.py reads this at
        # startup and hands us the map, but nothing told the UI which file it
        # was, so an entry list applied to every scan was invisible in
        # Settings -- there was no way to see what was loaded, and so no
        # obvious way to get rid of it.
        _state["map_path"] = settings.extra.get("map_path") or None
        _restore_watch(settings)
    if number_map is not None:
        _state["number_map"] = number_map


def _restore_watch(settings: Settings) -> None:
    """Pick a folder watch back up after a restart.

    This is the case worth persisting for: the copy is still running and
    Conrod is the thing that stopped. A watch that only lasted as long as the
    window would be off exactly when it was needed.
    """
    saved = settings.extra.get("watch") or {}
    if not saved.get("path") or saved.get("job_id") is None:
        return
    try:
        set_watch(WatchRequest(active=True, path=saved["path"],
                               job_id=saved["job_id"],
                               recursive=bool(saved.get("recursive", True)),
                               interval=float(saved.get("interval",
                                                        watch.DEFAULT_INTERVAL))))
    except Exception:
        # A folder that is no longer there -- the card came out -- must not
        # stop the application from starting.
        settings.extra.pop("watch", None)


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


_health: dict = {"at": 0.0, "value": None}
_health_lock = threading.Lock()


@app.get("/api/health")
def health() -> dict:
    """One light for the whole app: is anything missing or degraded?

    Cached, because it shells out to exiftool and asks Ollama what it has
    loaded, and the header polls it.
    """
    with _health_lock:
        fresh = _health["value"] and time.time() - _health["at"] < 20
        if fresh:
            return _health["value"]

    env = setup_check.inspect(_state["settings"])
    missing_required = [c for c in env.checks if c.required and not c.ok]
    missing_optional = [c for c in env.checks if not c.required and not c.ok]

    settings: Settings = _state["settings"]
    switched_off = [name for flag, name in (
        (settings.use_vlm, "Vision model"),
        (settings.read_plates, "Plate reading"),
    ) if not flag]

    if missing_required:
        level, summary = "error", missing_required[0].label + " is missing"
    elif missing_optional:
        level = "warn"
        summary = missing_optional[0].label + " unavailable"
    elif switched_off:
        # Everything is installed, but a reader is turned off, so the app is
        # producing less than it could. Saying "Ready" here would be misleading.
        level = "warn"
        summary = f"{switched_off[0]} is turned off"
    else:
        level, summary = "ok", "Everything is installed"

    if _run.get("error"):
        level, summary = "error", "The last scan failed"
    elif _run.get("active"):
        summary = "Scanning" + (" (paused)" if _run.get("paused") else "")

    value = {
        "level": level,
        "summary": summary,
        "scanning": bool(_run.get("active")),
        "paused": bool(_run.get("paused")),
        "problems": [{"label": c.label, "detail": c.detail, "required": c.required}
                     for c in missing_required + missing_optional]
                    + [{"label": name, "detail": "turned off in settings",
                        "required": False} for name in switched_off],
    }
    with _health_lock:
        _health.update({"at": time.time(), "value": value})
    return value


_update: dict = {"state": "idle", "message": "", "release": None,
                 "done": 0, "total": 0}
_update_lock = threading.Lock()


@app.get("/api/update/check")
def update_check() -> dict:
    """Is there a newer release on GitHub?"""
    from . import __version__, update as updater

    try:
        release = updater.check()
    except Exception as exc:
        return {"ok": False, "current": __version__,
                "error": f"could not reach GitHub: {exc}"}
    return {
        "ok": True, "current": __version__, "latest": release.version,
        "newer": release.newer, "url": release.url, "tag": release.tag,
        "size": release.size, "notes": release.notes[:1200],
        "installable": bool(release.asset) and bool(getattr(sys, "frozen", False)),
        "from_source": not getattr(sys, "frozen", False),
    }


# Set by desktop.launch so the app can close itself. An update swaps the
# folder the running executable lives in, which Windows will not allow while
# it is running, so quitting is a required step of installing -- not a
# courtesy. Without it the swap script waited sixty seconds for a process that
# was never going to exit, tried the move anyway, failed because the files
# were in use, rolled back, and left no trace but a progress bar that stopped.
_quit_hook = None


def set_quit_hook(fn) -> None:
    global _quit_hook
    _quit_hook = fn


def request_quit(delay: float = 1.5) -> None:
    """Close the app, after giving the reply time to reach the page."""
    def go() -> None:
        time.sleep(delay)
        if _quit_hook is not None:
            try:
                _quit_hook()
            except Exception:
                pass
        # The hook closes the window, which normally ends the process on its
        # own. If anything is still holding it, leave anyway: the update is
        # already staged and waiting on us.
        time.sleep(5)
        os._exit(0)

    threading.Thread(target=go, daemon=True).start()


@app.post("/api/update/install")
def update_install() -> dict:
    """Download the newest release and swap the app over to it.

    Only ever runs because someone pressed the button. The download is
    verified against the checksum published with the release before anything
    is unpacked, and a frozen build is required -- see update.py.
    """
    from . import update as updater

    # Refuse before downloading 300 MB, not after: install() checks this too,
    # but by then the bandwidth is spent.
    if not getattr(sys, "frozen", False):
        raise HTTPException(
            400, "running from source — update with 'git pull' instead")

    with _update_lock:
        if _update["state"] in ("downloading", "installing"):
            raise HTTPException(409, "an update is already in progress")
        _update.update({"state": "downloading", "message": "checking GitHub",
                        "done": 0, "total": 0})

    def worker() -> None:
        try:
            release = updater.check()
            if not release.newer:
                _update.update({"state": "idle", "message": "already up to date"})
                return
            _update["release"] = release.version

            started = {"at": time.monotonic(), "from": 0, "seen": False}

            def progress(done: int, total: int) -> None:
                # Timed from the first chunk, and from the byte it started at,
                # so a resumed download reports the speed it is getting now
                # rather than averaging in the part it did not fetch.
                if not started["seen"]:
                    started.update({"at": time.monotonic(), "from": done,
                                    "seen": True})
                elapsed = time.monotonic() - started["at"]
                rate = (done - started["from"]) / elapsed if elapsed > 1 else 0
                _update.update({
                    "done": done, "total": total, "rate": rate,
                    "eta": int((total - done) / rate) if rate > 0 else None,
                    "message": f"downloading {release.version}"})

            archive = updater.download(release, DATA_ROOT / "updates", progress)
            _update.update({"state": "installing", "message": "unpacking"})
            note(f"update: installing {release.version}")
            _update["message"] = updater.install(archive)
            _update["state"] = "restarting"
            # The swap cannot happen while this process holds the folder.
            request_quit()
        except Exception as exc:
            _update.update({"state": "error", "message": str(exc)})
            note(f"update failed: {exc}", "error")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


@app.get("/api/update/status")
def update_status() -> dict:
    return dict(_update)


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

    # Settings can change what the health light is reporting on -- which
    # vision model to look for, whether plates are read at all -- so the
    # cached answer is no longer valid.
    with _health_lock:
        _health["at"] = 0.0

    if body.map_path is not None:
        path = body.map_path.strip()
        if not path:
            _state["number_map"], _state["map_path"] = NumberMap(), None
            # Forgetting it in memory was not enough. The path stayed in the
            # saved settings, main.py loaded it again on the next launch, and
            # an entry list cleared in the UI came straight back -- so a CSV
            # loaded once could never be got rid of.
            settings.extra.pop("map_path", None)
        else:
            try:
                _state["number_map"] = NumberMap.load(Path(path))
                _state["map_path"] = path
                settings.extra["map_path"] = path
            except Exception as exc:
                raise HTTPException(400, f"could not read that CSV: {exc}")
        settings.save()

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
    # Which part of the work to do. Adding an album indexes it and stops;
    # culling and identifying are then separate decisions, because the
    # expensive one should not be the automatic one.
    stage: str = "all"          # all | index | cull | identify


@app.post("/api/scan")
def start_scan(body: ScanRequest) -> dict:
    with _run_lock:
        if _run["active"]:
            # Adding an album is not the thing the one-at-a-time rule is
            # protecting. That rule is about the GPU and the detector, and
            # indexing touches neither -- it walks the folder, reads EXIF
            # and pulls the previews out. Refusing it meant that during a
            # scan that runs for hours there was no way to add the next
            # card at all: the Scan screen only ever showed the run already
            # going, and "Add album" had nowhere to go.
            if body.stage == "index" and not _index["active"]:
                return _start_index(body)
            raise HTTPException(409, "a scan is already running")
        if body.stage not in ("all", "index", "cull", "identify"):
            raise HTTPException(400, f"unknown stage {body.stage!r}")
        root = Path(body.path)
        if body.stage == "identify":
            if body.resume_job is None:
                raise HTTPException(400, "identify continues an album, so it needs one")
            # On an album that has already been culled nothing is read off
            # disk, so the folder need not still exist -- the card may have
            # been unplugged weeks ago. On one that has only been indexed
            # there are no crops yet, so the photographs do have to be there.
            if _needs_folder_to_identify(body.resume_job) and not root.is_dir():
                raise HTTPException(
                    400, "no vehicles have been found in this album yet, so "
                         "identifying it means reading the photographs again "
                         f"-- and {body.path} is not there")
        elif not root.is_dir():
            raise HTTPException(400, "not a folder")
        _frames.clear()
        _rate.clear()
        _clear_live()
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
            culling = body.stage == "cull"
            record = {
                "name": frame.get("name"), "preview": frame.get("preview"),
                "boxes": boxes,
                "phase": _frame_phase(boxes, culling),
                "log": _frame_log(boxes, culling),
            }
            for box in boxes:
                _frames[box["id"]] = record
            # Trim the ring; a long scan would otherwise hold every frame.
            while len(_frames) > 400:
                _frames.pop(next(iter(_frames)))
            # A frame with vehicles normally waits for the vision model to
            # say something about them before it is shown. A cull never
            # calls the vision model, so nothing ever arrived and the live
            # view showed only the frames where nothing was found -- four
            # tiles reading NO VEHICLE while the counter beside them climbed
            # past fifty. The cull has plenty to say; it just was not asked.
            if not boxes or culling:
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
            if body.stage == "identify" and _has_detections(body.resume_job):
                # Naming an album that was detected and culled earlier. It
                # works from the stored crops, so there is no folder to walk
                # and no photograph to re-read.
                summary = pipeline.identify(
                    body.resume_job, _state["settings"], on_progress=progress,
                    should_stop=lambda: _run["stop"],
                    wait_if_paused=_resume_gate.wait,
                )
            else:
                summary = pipeline.run(
                    root, _state["settings"], label=body.label,
                    recursive=body.recursive, on_progress=progress,
                    should_stop=lambda: _run["stop"],
                    wait_if_paused=_resume_gate.wait,
                    resume_job=body.resume_job,
                    stop_after=None if body.stage in ("all", "identify")
                               else body.stage,
                    # Identify on an album nobody has culled: find the cars
                    # and name them, and keep every one of them. Culling is
                    # a separate decision and this is the photographer
                    # declining to make it yet.
                    cull=body.stage != "identify",
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
            # The one-line message is what the activity panel shows, but a
            # scan dying with "database is locked" and no traceback is close
            # to undebuggable. Keep the stack somewhere it can be read.
            try:
                import traceback

                from .config import LOG_PATH

                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    fh.write(f"\n--- scan failed {stamp} ---\n")
                    traceback.print_exc(file=fh)
            except Exception:
                pass
        finally:
            _run["active"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


# Recent (time, frames done) samples, for the estimate below.
_rate: deque = deque(maxlen=400)

ETA_WINDOW = 240.0      # seconds of history the estimate is drawn from
ETA_MIN_FRAMES = 6      # below this it is guessing, not estimating
ETA_MIN_SECONDS = 25.0


def _note_rate(done: int) -> None:
    if not _rate or _rate[-1][1] != done:
        _rate.append((time.time(), done))


def _estimate_eta() -> int | None:
    """Seconds remaining, or None while there is not enough to go on.

    Measured over a rolling window rather than the whole run. Dividing by the
    time since the scan started counts the minutes spent enumerating, culling
    and extracting previews as though frames had been analysed during them,
    and a fresh 6,000-frame scan announced "about 86.2 h left" off its first
    four frames. A window also lets the estimate follow a shoot that speeds up
    or slows down instead of being anchored to how it began.
    """
    samples = list(_rate)
    if len(samples) < 2:
        return None

    now, done = samples[-1]
    window = [s for s in samples if now - s[0] <= ETA_WINDOW]
    if len(window) < 2:
        return None

    started_at, started_done = window[0]
    frames = done - started_done
    span = now - started_at
    if frames < ETA_MIN_FRAMES or span < ETA_MIN_SECONDS:
        return None

    remaining = max(0, _run["total"] - done)
    if not remaining:
        return 0
    return round(remaining * span / frames)


# An album being added while a scan runs. Kept apart from _run on purpose:
# _run owns the live frame view, the pause gate and the stop flag, none of
# which mean anything to a folder being indexed, and all of which would be
# wrong to hand a second job a share of.
_index: dict = {"active": False, "job_id": None, "done": 0, "total": 0,
                "message": "", "error": None, "label": None}


def _start_index(body: ScanRequest) -> dict:
    """Index a folder alongside a running scan. Disk and CPU only."""
    root = Path(body.path)
    if not root.is_dir():
        raise HTTPException(400, "not a folder")
    _index.update({"active": True, "job_id": None, "done": 0, "total": 0,
                   "message": "reading the folder", "error": None,
                   "label": body.label or root.name})

    def progress(event: dict) -> None:
        # Indexing stops before detection, so it never emits the frame and
        # vehicle events the live view is built on -- only counts.
        if event.get("stage") not in ("frame", "vehicle"):
            _index["done"] = event.get("done", _index["done"])
            _index["total"] = event.get("total", _index["total"])
            if event.get("message"):
                _index["message"] = event["message"]

    def worker() -> None:
        try:
            summary = pipeline.run(
                root, _state["settings"], label=body.label,
                recursive=body.recursive, on_progress=progress,
                resume_job=body.resume_job, stop_after="index",
            )
            _index["job_id"] = summary.job_id
            _index["message"] = f"{summary.images} frames ready to cull"
            note(f"album added: {_index['message']}", "info")
        except Exception as exc:
            _index["error"] = f"{type(exc).__name__}: {exc}"
            note(_index["error"], "error")
        finally:
            _index["active"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "indexing": True}


@app.get("/api/scan")
def scan_status() -> dict:
    elapsed = time.time() - _run["started"] if _run["started"] else 0
    out = dict(_run)
    out["live"] = _live_frames()
    out["elapsed"] = round(elapsed, 1)
    out["eta"] = None
    if _run["active"] and _run["total"]:
        _note_rate(_run["done"])
        out["eta"] = _estimate_eta()
    # An album added while this one runs, so the Scan screen can say so
    # without the two of them fighting over the same progress bar.
    out["indexing"] = dict(_index)
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


# --- watching a folder ----------------------------------------------------
# A watch is a resume that happens on its own. Frames that land in the folder
# after the scan started get added to the same album, which is the ordinary
# case of a card still copying while the shoot is being packed up.

_watch: dict[str, Any] = {
    "active": False, "folder": None, "job_id": None, "recursive": True,
    "interval": watch.DEFAULT_INTERVAL, "added": 0, "checked": 0.0,
    "message": "",
    # Bumped every time a watch is set up. Turning one off and straight back
    # on would otherwise leave two loops polling the same folder, and both
    # would try to start the same resume.
    "generation": 0,
}
_watcher: watch.Watcher | None = None
_watch_wake = threading.Event()


def _stage_for_album(job_id: int) -> str:
    """How far a watched album is being taken, so new frames match the rest."""
    try:
        conn = store.connect()
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return "all"
    status = (row["status"] if row else "") or ""
    if status == "indexed":
        return "index"
    if status == "culled":
        return "cull"
    return "all"


def _known_frames(job_id: int) -> set[str]:
    """Every frame the album already holds, however its path is spelled."""
    conn = store.connect()
    try:
        return {watch.key(r["path"]) for r in conn.execute(
            "SELECT path FROM images WHERE job_id=?", (job_id,))}
    finally:
        conn.close()


def _watch_loop(generation: int) -> None:
    while _watch["active"] and _watch["generation"] == generation:
        # Interruptible, so turning the watch off is immediate rather than
        # up to a minute later.
        _watch_wake.wait(timeout=float(_watch["interval"]))
        _watch_wake.clear()
        if not _watch["active"] or _watch["generation"] != generation:
            break
        if _run["active"] or _watcher is None:
            continue                     # a scan is already using the folder
        try:
            found = _watcher.poll(known=_known_frames(_watch["job_id"]))
            _watch["checked"] = time.time()
            if not found:
                continue
            _watch["added"] += len(found)
            _watch["message"] = (f"{len(found)} new frame"
                                 f"{'' if len(found) == 1 else 's'}")
            note(f"watch: {_watch['message']}, continuing the album", "info")
            # The pipeline lists the folder itself and skips what is already
            # done, so handing it the folder is enough -- and means a watched
            # scan and a resumed one are the same code path.
            # Continue the album the way it is being worked. An album that
            # has only been indexed must not have new frames quietly run
            # through the vision model because a watch picked them up --
            # staging the work was a deliberate choice and a watch does not
            # get to undo it.
            start_scan(ScanRequest(path=str(_watch["folder"]),
                                   recursive=bool(_watch["recursive"]),
                                   resume_job=_watch["job_id"],
                                   stage=_stage_for_album(_watch["job_id"])))
        except Exception as exc:         # a watch must not die on one bad pass
            _watch["message"] = f"{type(exc).__name__}: {exc}"
            note(f"watch: {_watch['message']}", "warn")


class WatchRequest(BaseModel):
    active: bool
    path: str | None = None
    job_id: int | None = None
    recursive: bool = True
    interval: float = watch.DEFAULT_INTERVAL


@app.get("/api/watch")
def watch_status() -> dict:
    out = dict(_watch)
    out["folder"] = str(out["folder"]) if out["folder"] else None
    return out


@app.post("/api/watch")
def set_watch(body: WatchRequest) -> dict:
    """Turn folder monitoring on or off."""
    global _watcher
    settings: Settings = _state["settings"]

    if not body.active:
        _watch.update({"active": False, "message": "",
                       "generation": _watch["generation"] + 1})
        _watcher = None
        _watch_wake.set()
        settings.extra.pop("watch", None)
        settings.save()
        return watch_status()

    folder = Path(body.path or "")
    if not folder.is_dir():
        raise HTTPException(400, "not a folder")
    if body.job_id is None:
        raise HTTPException(400, "a watch continues an album, so it needs one")

    _watcher = watch.Watcher(folder, recursive=body.recursive)
    generation = _watch["generation"] + 1
    _watch.update({"active": True, "folder": folder, "job_id": body.job_id,
                   "recursive": body.recursive,
                   "interval": max(10.0, float(body.interval)),
                   "added": 0, "checked": 0.0, "message": "waiting",
                   "generation": generation})
    # Remembered so a watch survives closing the application, which is the
    # case that matters: the copy is still running and Conrod is not.
    settings.extra["watch"] = {"path": str(folder), "job_id": body.job_id,
                               "recursive": body.recursive,
                               "interval": _watch["interval"]}
    settings.save()
    threading.Thread(target=_watch_loop, args=(generation,), daemon=True).start()
    note(f"watch: monitoring {folder}", "info")
    return watch_status()


@app.get("/api/log")
def read_log(after: float = 0.0) -> dict:
    """What the scan has been doing. Everything the console would have shown."""
    with _journal_lock:
        lines = [e for e in _journal if e["at"] > after]
    return {"lines": lines, "at": lines[-1]["at"] if lines else after}


def _frame_phase(boxes: list, culling: bool) -> str:
    if not boxes:
        return "NO VEHICLE"
    return "CHECKING SHARPNESS" if culling else "VEHICLES FOUND"


def _frame_log(boxes: list, culling: bool) -> list[str]:
    """What was done to this frame, in the order it happened.

    During a cull this is the whole story -- there is no vision model to add
    to it later -- so it says what the cull measured and what it decided,
    rather than the one line about detection it used to say.
    """
    if not boxes:
        return ["no vehicle found in this frame"]

    plural = "" if len(boxes) == 1 else "s"
    lines = [f"{len(boxes)} vehicle{plural} detected"]
    if not culling:
        return lines

    lines.append("checking sharpness on each vehicle")
    for box in boxes:
        kind = box.get("kind") or "vehicle"
        if box.get("culled"):
            lines.append(f"{kind}: too soft — culled")
        elif box.get("panning"):
            # A held pan is blur in the background, not on the car, and is
            # never culled automatically. Saying so is the difference
            # between "it understood the shot" and "it got it wrong".
            lines.append(f"{kind}: panning shot — kept")
        elif box.get("rating"):
            stars = box.get("stars")
            grade = (f"{stars} star{'' if stars == 1 else 's'}" if stars
                     else box["rating"])
            lines.append(f"{kind}: {box.get('focus') or box['rating']} — {grade}")
        else:
            lines.append(f"{kind}: kept")
    return lines


@app.get("/api/scan/frame")
def scan_frame(t: int = 0):
    """The preview of a frame currently being scanned.

    Served by token rather than path so the browser refetches when the frame
    changes but caches within a frame, and so no arbitrary path can be read.
    """
    preview = _preview_for(t) if t else _run.get("preview")
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


class JobPatch(BaseModel):
    label: str | None = None


@app.post("/api/jobs/{job_id}")
def rename_job(job_id: int, body: JobPatch) -> dict:
    with store.session() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "no such scan")
        conn.execute("UPDATE jobs SET label=? WHERE id=?",
                     ((body.label or "").strip() or None, job_id))
    return {"ok": True}


def _needs_folder_to_identify(job_id: int | None) -> bool:
    """Whether identifying this album means reading the photographs again.

    Only true for an album that exists and has nothing found in it yet. An
    album nobody recognises is not a folder problem and must not be reported
    as one -- the run itself gives a straight answer about the missing job,
    where a complaint about the path sends you looking for a card that was
    never the issue.
    """
    if job_id is None:
        return False
    with store.session() as conn:
        known = conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(known) and not _has_detections(job_id)


def _has_detections(job_id: int | None) -> bool:
    """Whether there is anything stored to identify without re-reading files.

    Decides which of two quite different jobs "identify" means: naming crops
    that a cull already cut, or walking the folder to find the cars first.
    An album that has only been indexed has no crops, and pointing the crop
    reader at it finds nothing and reports success.
    """
    if job_id is None:
        return False
    with store.session() as conn:
        return bool(conn.execute(
            """SELECT 1 FROM detections d JOIN images i ON i.id = d.image_id
                WHERE i.job_id = ? LIMIT 1""", (job_id,)).fetchone())


@app.post("/api/reset/identifications")
def reset_identifications(job_id: int | None = None) -> dict:
    """Forget what the vision model said, and keep everything else.

    The narrowest of the three resets, and the one that recovers a run
    where the model itself was the problem -- a wrong model name, a
    credential that could not call the endpoint, a provider down for the
    afternoon. Identify only looks at detections that have never been
    answered, so an album full of *empty* answers is finished as far as it
    is concerned: running it again reads nothing and reports success. This
    is what makes it readable again.

    Deliberately not the same as resetting detections. That one deletes the
    detections and their crops, which takes the ratings with them -- and
    the ratings are the hand-given stars the learned rating is fitted to,
    which cost an afternoon to give and cannot be recomputed. Kept here:
    every crop, every star, sharpness, framing, plates, race numbers,
    embeddings, and which frames were rejected. Dropped: the make, model,
    colour, team, sponsors and livery text, and the groups built out of
    them.

    The photographs are untouched, as always.
    """
    if _run.get("active"):
        raise HTTPException(
            409, "A scan is running. Stop it first, then reset.")

    where = "WHERE i.job_id = ?" if job_id is not None else ""
    args = (job_id,) if job_id is not None else ()
    with store.session() as conn:
        if job_id is not None and not conn.execute(
                "SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "no such scan")
        cleared = conn.execute(
            f"""UPDATE detections
                   SET attributes = NULL,
                       group_key = NULL, group_size = NULL,
                       group_agreement = NULL, group_colour_hex = NULL
                 WHERE image_id IN (SELECT i.id FROM images i {where})""",
            args).rowcount
        # A number the vision model read goes with it; one the OCR or the
        # roundel reader found does not. Those never involved the model and
        # are usually the better reading anyway.
        conn.execute(
            f"""UPDATE detections SET number = NULL, number_source = NULL,
                       number_conf = NULL
                 WHERE number_source = 'vlm' AND image_id IN
                       (SELECT i.id FROM images i {where})""", args)
        # Back to "culled": the cars have been found and judged, and none of
        # them has been named. Which is exactly what Identify expects.
        conn.execute(
            "UPDATE jobs SET status='culled'"
            + (" WHERE id=?" if job_id is not None else ""), args)
        conn.commit()
    return {"ok": True, "identifications_cleared": cleared}


@app.post("/api/reset/detections")
def reset_detections(job_id: int | None = None) -> dict:
    """Throw away every detection and identification, keeping the albums.

    The expensive half of a scan is finding and naming the cars; the cheap
    half is walking the folder and pulling previews out of the RAWs. This
    drops the first and keeps the second, so re-running does not spend
    minutes re-reading files that have not changed. Which is what "start the
    identification again" actually means -- the alternative, forgetting the
    album outright, makes you re-index a shoot whose frames are all still
    exactly where they were.

    The photographs are untouched, as always. ``job_id`` limits it to one
    album; without it, every album.
    """
    if _run.get("active"):
        raise HTTPException(
            409, "A scan is running. Stop it first, then reset.")

    where = "WHERE i.job_id = ?" if job_id is not None else ""
    args = (job_id,) if job_id is not None else ()
    with store.session() as conn:
        if job_id is not None and not conn.execute(
                "SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "no such scan")
        crops = [r["crop_path"] for r in conn.execute(
            f"""SELECT d.crop_path FROM detections d
                  JOIN images i ON i.id = d.image_id
                 {where} {'AND' if where else 'WHERE'} d.crop_path IS NOT NULL""",
            args)]
        gone = conn.execute(
            f"""DELETE FROM detections WHERE image_id IN
                (SELECT i.id FROM images i {where})""", args).rowcount
        # Back to "indexed": the frames are known, nothing has been found in
        # them yet. Leaving them "done" would offer a Write XMP that has
        # nothing left to write.
        conn.execute(
            "UPDATE jobs SET status='indexed'"
            + (" WHERE id=?" if job_id is not None else ""), args)
        conn.commit()

    removed = 0
    for path in crops:
        for candidate in (Path(path), *Path(path).parent.glob(
                Path(path).stem + ".t*.jpg")):
            try:
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return {"ok": True, "detections_removed": gone, "crops_removed": removed}


@app.post("/api/reset")
def reset_everything() -> dict:
    """Forget every scan and everything read from it, and start again.

    The photographs are not touched. Nothing Conrod has decided has ever been
    written to them -- that only happens on Write XMP -- so this throws away
    Conrod's own work and nothing else: the scans, the detections, the plates
    and numbers and identifications, and the crops cut out along the way.
    Settings and the entry list are kept, because they are how the next scan
    is set up rather than a result of the last one.

    Refuses while a scan is running rather than tearing the ground out from
    under it. The caller is expected to have asked first; a confirmation is
    not something a server can check, so this at least makes the irreversible
    half impossible to reach by accident.
    """
    if _run.get("active"):
        raise HTTPException(
            409, "A scan is running. Stop it first, then reset.")

    with store.session() as conn:
        crops = [r["crop_path"] for r in conn.execute(
            "SELECT crop_path FROM detections WHERE crop_path IS NOT NULL")]
        jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        frames = conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"]
        # ON DELETE CASCADE clears images and detections with the jobs.
        conn.execute("DELETE FROM jobs")

    removed = 0
    for path in crops:
        for candidate in (Path(path), *Path(path).parent.glob(
                Path(path).stem + ".t*.jpg")):
            try:
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass

    # Back to the values it starts life with, rather than cleared: the rest
    # of the server reads these keys by name and an empty dict is a KeyError
    # waiting for whoever next opens the scan page.
    _index.update({"active": False, "job_id": None, "done": 0, "total": 0,
                   "message": "", "error": None, "label": None})
    return {"ok": True, "scans_removed": jobs, "frames_removed": frames,
            "crops_removed": removed}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int) -> dict:
    """Forget a scan.

    Only the record: the photos are never touched, and the extracted previews
    are left alone because another scan of the same folder would just have to
    make them again.
    """
    with store.session() as conn:
        row = conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such scan")
        crops = [r["crop_path"] for r in conn.execute(
            """SELECT d.crop_path FROM detections d
                 JOIN images i ON i.id = d.image_id
                WHERE i.job_id = ? AND d.crop_path IS NOT NULL""", (job_id,))]
        # ON DELETE CASCADE clears images and detections with the job.
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    removed = 0
    for path in crops:
        for candidate in (Path(path), *Path(path).parent.glob(
                Path(path).stem + ".t*.jpg")):
            try:
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return {"ok": True, "crops_removed": removed}


@app.get("/api/jobs/{job_id}/frames")
def job_frames(job_id: int, offset: int = 0,
               limit: int = Query(200, le=500)) -> dict:
    """The album's frames, for a contact sheet.

    An album that has only been indexed has no vehicles and no crops, so the
    review screen -- which is a grid of vehicles -- has nothing to show and
    the album looks empty when it is not. This is the frames themselves,
    which exist from the moment the folder is read.
    """
    with store.session() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "no such album")
        total = conn.execute("SELECT COUNT(*) FROM images WHERE job_id=?",
                             (job_id,)).fetchone()[0]
        rows = conn.execute(
            """SELECT i.id, i.path, i.status, i.preview_path, i.written_at,
                      COUNT(d.id)                        AS vehicles,
                      SUM(CASE WHEN d.rejected THEN 1 ELSE 0 END) AS cut,
                      MAX(d.rating)                      AS rating,
                      -- The frame's best surviving vehicle decides how the
                      -- frame reads, which is the same rule write_job uses
                      -- to pick the rating it writes.
                      (SELECT d2.rating_verdict FROM detections d2
                        WHERE d2.image_id = i.id AND d2.rejected = 0
                        ORDER BY d2.rating DESC LIMIT 1) AS verdict
                 FROM images i
                 LEFT JOIN detections d ON d.image_id = i.id
                WHERE i.job_id = ?
                GROUP BY i.id
                ORDER BY i.id
                LIMIT ? OFFSET ?""", (job_id, limit, offset)).fetchall()

    frames = []
    for row in rows:
        preview = row["preview_path"] or ""
        vehicles = row["vehicles"] or 0
        kept = vehicles - (row["cut"] or 0)
        frames.append({
            "id": row["id"],
            "name": Path(row["path"]).name,
            "status": row["status"],
            # Only a JPEG preview can be shown; a RAW with none yet cannot.
            "viewable": preview.lower().endswith((".jpg", ".jpeg")),
            "vehicles": vehicles,
            "kept": kept,
            "verdict": row["verdict"],
            "label": (sharpness_mod.label_for(row["verdict"])
                      if row["verdict"] else None),
            "rating": row["rating"],
            "written": bool(row["written_at"]),
        })
    return {"total": total, "offset": offset, "frames": frames}


@app.get("/api/jobs/{job_id}/summary")
def job_summary(job_id: int) -> dict:
    settings: Settings = _state["settings"]
    with store.session() as conn:
        conn.create_function("_needs_review", 5, _needs_review)
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
                                             d.rejected, d.uncertain, :thresh) = 1
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

        plates = conn.execute(
            """SELECT d.plate AS plate, COUNT(*) AS frames
                 FROM detections d JOIN images i ON i.id = d.image_id
                WHERE i.job_id = ? AND d.plate IS NOT NULL AND d.rejected = 0
                GROUP BY d.plate ORDER BY frames DESC""",
            (job_id,),
        ).fetchall()

        number_map: NumberMap = _state["number_map"]
        return {
            "job": dict(job),
            "counts": {k: (v or 0) for k, v in dict(counts).items()},
            "images": {k: (v or 0) for k, v in dict(images).items()},
            "numbers": [{**dict(n), "who": number_map.describe(n["number"])}
                        for n in numbers],
            "plates": [dict(p) for p in plates],
            "map_size": len(number_map),
        }


# --- detections -----------------------------------------------------------

DETECTION_QUERY = """
SELECT d.id, d.number, d.number_source, d.number_conf, d.conf, d.cls,
       d.plate, d.plate_state, d.plate_conf, d.attributes,
       d.reviewed, d.rejected, d.group_key, d.group_size, d.group_agreement,
       d.colour_hex, d.group_colour_hex, d.sharpness, d.sharpness_verdict,
       d.cull_reason, d.clipped, d.rating, d.rating_verdict, d.stars,
       d.predicted_stars,
       d.panning, d.background, d.sharp_end, d.uncertain, d.bystander,
       i.path AS image_path, i.id AS image_id, i.camera, i.burst_key,
       -- Whether "which car is the subject" is even a question for this
       -- frame. One vehicle in it and there is nothing to choose between.
       (SELECT COUNT(*) FROM detections x
         WHERE x.image_id = d.image_id) AS in_frame
  FROM detections d
  JOIN images i ON i.id = d.image_id
 WHERE i.job_id = :job_id
"""


# A rating given by hand outranks the measured one here too, so a frame
# the photographer starred sorts where they put it rather than where the
# sharpness measure thought it belonged. Both are on the same 1-5 scale.
# NULLs last in both directions: an unrated frame is not a bad one, and
# burying it under the rejects is how it never gets looked at.
def _rank_sql() -> str:
    """Stars, in SQL, from the same bands the rest of the app uses.

    Written out rather than imported because the sort happens in SQLite.
    Generated from STAR_BANDS so that retuning the bands cannot leave the
    sort ordering by numbers nothing else agrees with.

    Three sources, in the order they deserve to be believed: the star the
    photographer gave, then the one learned from the stars they have given
    elsewhere, then the focus measure. A prediction never overrules an
    answer, and the measure is what is left when there is nothing to learn
    from -- a new machine, or an album nobody has rated yet.
    """
    arms = " ".join(f"WHEN d.rating >= {floor} THEN {stars}"
                    for floor, stars in sharpness_mod.STAR_BANDS)
    return ("COALESCE(d.stars, d.predicted_stars, "
            f"CASE WHEN d.rating IS NULL THEN NULL {arms} END)")


_RANK = _rank_sql()

ORDERINGS = {
    # The default: least confident first, because that is what review is for.
    "review": "d.number_conf ASC, d.id ASC",
    # `d.stars IS NULL` before the raw rating so that at equal stars a
    # rating given by hand comes first. It is a judgement where the measured
    # one is a proposal, and now that the measure reaches five as well, a
    # frame someone starred was being buried under a sharper one that had
    # merely been calculated to the same number.
    "best": f"({_RANK}) IS NULL, ({_RANK}) DESC, d.stars IS NULL, d.rating DESC, d.id ASC",
    "worst": f"({_RANK}) IS NULL, ({_RANK}) ASC, d.stars IS NULL, d.rating ASC, d.id ASC",
    "frame": "i.id ASC, d.id ASC",
}


@app.get("/api/jobs/{job_id}/detections")
def detections(
    job_id: int,
    view: str = Query("review", pattern="^(review|all|number|plate|rejected)$"),
    number: str | None = None,
    plate: str | None = None,
    search: str | None = None,
    sort: str = Query("review", pattern="^(review|best|worst|frame)$"),
    min_stars: int | None = Query(None, ge=1, le=5),
    limit: int = 120,
    offset: int = 0,
) -> dict:
    settings: Settings = _state["settings"]
    clauses: list[str] = []
    params: dict[str, Any] = {"job_id": job_id, "limit": limit, "offset": offset}

    if view == "review":
        clauses.append(
            "_needs_review(d.number_conf, d.reviewed, d.rejected, d.uncertain,"
            " :thresh) = 1"
        )
        params["thresh"] = settings.ocr_accept_confidence
    elif view == "number":
        if not number:
            raise HTTPException(400, "view=number needs a number")
        clauses.append("d.number = :number AND d.rejected = 0")
        params["number"] = number
    elif view == "plate":
        if plate:
            clauses.append("d.plate = :plate AND d.rejected = 0")
            params["plate"] = plate
        else:
            clauses.append("d.plate IS NOT NULL AND d.rejected = 0")
    elif view == "rejected":
        clauses.append("d.rejected = 1")

    if search:
        clauses.append(
            "(IFNULL(d.attributes,'') LIKE :q OR IFNULL(d.number,'') LIKE :q "
            "OR IFNULL(d.plate,'') LIKE :q OR i.path LIKE :q)"
        )
        params["q"] = f"%{search}%"

    # Aftershoot-style "3 stars and up" as a filter, not just a sort order --
    # the same effective-stars expression the sort already uses, so a card
    # showing 4 stars and a filter set to 3+ can never disagree.
    if min_stars:
        clauses.append(f"({_RANK}) >= :min_stars")
        params["min_stars"] = min_stars

    sql = DETECTION_QUERY
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += f" ORDER BY {ORDERINGS[sort]} LIMIT :limit OFFSET :offset"

    with store.session() as conn:
        conn.create_function("_needs_review", 5, _needs_review)
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
        # The stored analysis only carries a real "kind" once identify() has
        # actually run; before that it is the dataclass default, "car" --
        # regardless of what the detector actually found. The detection's
        # own class is ground truth from the moment it exists, so it wins
        # over an unset or stale one. Without this a motorcycle waiting to
        # be identified showed "Car" on the card, not "Motorcycle".
        if item.get("cls"):
            analysis.kind = item["cls"]
        item["filename"] = Path(item["image_path"]).name
        item["who"] = number_map.describe(item["number"]) if item["number"] else ""
        item["crop_url"] = f"/api/crop/{item['id']}"
        item["frame_url"] = f"/api/frame/{item['image_id']}"
        item["attributes"] = analysis.to_dict()
        # Read straight from the stored JSON: the disputed list is written by
        # grouping and is not a field of VehicleAnalysis, so round-tripping
        # through it would drop the list silently.
        try:
            item["disputed"] = (json.loads(raw_attributes or "{}")
                                .get("group_disputed") or [])
        except (TypeError, ValueError):
            item["disputed"] = []
        item["title"] = analysis.title
        # Sent separately as well as inside the title, so the review card can
        # tell "the group agreed on Ford but not the model" apart from "the
        # group agreed on nothing" without parsing the title back apart.
        item["make"] = analysis.make
        item["model"] = analysis.model
        # The sampled paint, so the card can show a square of the actual
        # colour next to the model's word for it. The two disagree often
        # enough that the word alone is not much use.
        # The group's agreed colour where there is one, otherwise this
        # frame's own sample. Both are kept in the database.
        item["colour_hex"] = (item.pop("group_colour_hex", None)
                              or item.get("colour_hex"))
        item["colour_word"] = analysis.colour
        # Measured on the crop, so a panning shot is judged on its subject
        # rather than on the blur that makes it worth keeping.
        item["sharpness"] = item.get("sharpness")
        item["sharpness_verdict"] = item.get("sharpness_verdict") or ""
        # Why it was cut, if it was cut by Conrod rather than by a person.
        item["cull_reason"] = item.get("cull_reason") or ""
        item["rating_verdict"] = item.get("rating_verdict") or ""
        item["clipped"] = item.get("clipped") or 0
        # Where the sharpness is, not just how much. A pan is a keeper whose
        # numbers look like a reject, so the card has to be able to say so.
        item["panning"] = bool(item.get("panning"))
        item["sharp_end"] = item.get("sharp_end") or "even"
        item["background"] = item.get("background")
        item["uncertain"] = bool(item.get("uncertain"))
        # The stars the catalogue will get, so the card and the sidecar
        # cannot disagree about how good the frame is. A rating given by
        # hand wins: the measurement is a proposal, the photographer's is
        # the answer.
        # Same order the sort uses: the answer, then what was learned from
        # their other answers, then the measure.
        rating_value = item.get("rating")
        item["by_hand"] = item.get("stars") is not None
        item["learned"] = (not item["by_hand"]
                           and item.get("predicted_stars") is not None)
        item["stars"] = (item.get("stars") or item.get("predicted_stars") or (
            None if rating_value is None else sharpness_mod.stars_for(rating_value)))
        try:
            item["second_look"] = bool(json.loads(raw_attributes or "{}")
                                       .get("group_second_look"))
        except (TypeError, ValueError):
            item["second_look"] = False
        item["keywords"] = keywords_mod.for_vehicle(analysis, settings, number_map)
        items.append(item)
    return {"total": total, "items": items}


def _needs_review(number_conf, reviewed, rejected, uncertain, threshold) -> int:
    """A detection a human should still look at.

    Registered as a SQL function so the summary count and the review grid
    cannot drift apart — they were inconsistent when this logic was duplicated.

    A culled frame normally leaves review: it has been dealt with. The
    exception is a cull the measurement was not sure about -- a pan held on
    one end of the car reads as blurred when it is averaged over the whole
    vehicle, and a shoot that silently loses those is worse than one that
    culls nothing. Those come back into review until someone has looked.
    """
    if reviewed:
        return 0
    if rejected:
        return 1 if uncertain else 0
    return 1 if (number_conf is None or number_conf < (threshold or 0.8)) else 0


class DetectionUpdate(BaseModel):
    number: str | None = None
    plate: str | None = None
    attributes: dict | None = None
    rejected: bool | None = None
    bystander: bool | None = None
    reviewed: bool = True
    # 1-5 by hand, or 0 to hand the frame back to the measured rating.
    stars: int | None = Field(default=None, ge=0, le=5)


@app.post("/api/taste")
def learn_taste() -> dict:
    """Learn this photographer's scale from the frames they have rated.

    Their ratings are their ratings whichever album they were given on, so
    this is deliberately not per-job. Returns how well the result agrees with
    ratings it was not shown, which is the only figure worth quoting.
    """
    from . import pipeline

    score = pipeline.learn_taste(_state["settings"])
    if not score:
        from . import taste as taste_mod

        raise HTTPException(
            400, f"{taste_mod.ENOUGH_RATINGS} rated frames are needed before "
                 "Conrod can learn your scale. Rate some in review and try "
                 "again.")
    return score


@app.post("/api/jobs/{job_id}/group")
def group_cars(job_id: int) -> dict:
    """Sort an album into one pile per car.

    Works from the stored crops, so it does not re-read a single photograph,
    and does not call the vision model at all -- which frames show one car is
    a question about what the crops look like, and the similarity model
    answers it without a metered API in the loop.

    An album scanned before the similarity model existed has no embeddings,
    so they are filled in first. That is the slow part, it happens once, and
    without it grouping quietly falls back to the much cruder measure and
    nobody is told why the piles are wrong.
    """
    from . import grouping, pipeline

    looked = pipeline.embed_missing(job_id, _state["settings"])
    with store.session() as conn:
        out = grouping.consolidate(conn, job_id, _state["settings"])
    out["looked"] = looked
    return out


@app.post("/api/jobs/{job_id}/regroup")
def regroup(job_id: int) -> dict:
    """The old name for group_cars, kept so an older window still works."""
    return group_cars(job_id)


@app.post("/api/detections/{det_id}")
def update_detection(det_id: int, body: DetectionUpdate) -> dict:
    settings: Settings = _state["settings"]
    with store.session() as conn:
        row = conn.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such detection")

        analysis = VehicleAnalysis.from_json(row["attributes"])
        if row["cls"]:
            analysis.kind = row["cls"]
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
            # Sponsors are a list, not a string -- the review UI edits them
            # as chips, so what arrives is the whole list as it should now
            # stand rather than one value to set. Blanks and duplicates are
            # dropped here so the same name typed twice, or with different
            # spacing, cannot end up as two keywords in the catalogue.
            if "sponsors" in body.attributes:
                seen, cleaned = set(), []
                for raw in body.attributes["sponsors"] or []:
                    text = str(raw).strip()
                    if text and text.upper() not in seen:
                        seen.add(text.upper())
                        cleaned.append(text)
                analysis.sponsors = cleaned

        rejected = row["rejected"] if body.rejected is None else int(body.rejected)
        bystander = (row["bystander"] if body.bystander is None
                     else int(body.bystander))
        # 0 means "forget what I said", not "zero stars" -- every catalogue
        # reads 0 as unrated, and there has to be a way back to the measured
        # rating after a mis-keyed number.
        stars = row["stars"]
        if body.stars is not None:
            stars = body.stars or None
        conn.execute(
            """UPDATE detections
                  SET number=?, number_source=?, number_conf=?,
                      plate=?, attributes=?, rejected=?, reviewed=?, stars=?,
                      bystander=?
                WHERE id=?""",
            (number, source, confidence, plate, analysis.to_json(),
             rejected, int(body.reviewed), stars, bystander, det_id),
        )

    number_map: NumberMap = _state["number_map"]
    return {
        "ok": True, "id": det_id, "number": number, "plate": plate,
        "bystander": bool(bystander),
        # The stars the card should now show, which after clearing a hand
        # rating is the measured one -- not the empty column. Returning the
        # column left the pill reading "-" on a frame the cull had rated.
        "stars": stars or row["predicted_stars"] or (
            None if row["rating"] is None
            else sharpness_mod.stars_for(row["rating"])),
        "by_hand": stars is not None,
        "who": number_map.describe(number) if number else "",
        "title": analysis.title,
        "keywords": keywords_mod.for_vehicle(analysis, settings, number_map),
    }


class BulkUpdate(BaseModel):
    ids: list[int]
    number: str | None = None
    rejected: bool | None = None
    bystander: bool | None = None


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


THUMB_EDGE = 420
THUMB_DIR = CACHE_DIR / "thumbs"


@app.get("/api/thumb/{image_id}")
def thumb(image_id: int) -> FileResponse:
    """A contact-sheet sized copy of a frame.

    The stored preview is JpgFromRaw: 4640x6960 and about 6MB, chosen at that
    size deliberately because a registration plate has to survive in it. A
    200-tile sheet of those is 1.2GB over the wire, which is exactly how the
    album screen behaved -- tiles arriving one at a time and the page unable
    to finish rendering.

    Cached on disk beside the previews, and keyed by the preview's own
    modification time so a re-extracted frame is not served stale.
    """
    with store.session() as conn:
        row = conn.execute("SELECT preview_path, path FROM images WHERE id=?",
                           (image_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such image")
    source = Path(row["preview_path"] or row["path"])
    if not source.exists() or source.suffix.lower() not in {".jpg", ".jpeg"}:
        raise HTTPException(404, "no viewable preview")

    cached = THUMB_DIR / f"{image_id}.jpg"
    try:
        make_thumb(source, cached)
    except OSError as exc:
        raise HTTPException(404, f"could not make a thumbnail: {exc}")

    return FileResponse(cached, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


def make_thumb(source: Path, cached: Path, edge: int = THUMB_EDGE) -> Path:
    """Shrink one preview, unless a current thumbnail is already there."""
    if cached.exists() and cached.stat().st_mtime >= source.stat().st_mtime:
        return cached

    cached.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        # draft() lets libjpeg decode straight to roughly the size wanted by
        # skipping DCT coefficients. On a 4640x6960 frame that is the
        # difference between decoding 32 megapixels and about half of one.
        image.draft("RGB", (edge, edge))
        image = image.convert("RGB")
        image.thumbnail((edge, edge), Image.LANCZOS)
        # Written under a temporary name and moved into place: two requests
        # for the same new thumbnail arrive together, and a half-written JPEG
        # served to the other one is a broken tile that never heals.
        staging = cached.with_suffix(f".{os.getpid()}.tmp")
        image.save(staging, "JPEG", quality=78, optimize=True)
        staging.replace(cached)
    return cached


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
