"""The orchestrator: folder in, keyworded frames out.

Stages run overlapped where it pays. Detection is CPU-bound and the vision
model is GPU-bound, so analysis runs on its own thread and consumes crops as
detection produces them rather than waiting for the whole shoot to be scanned.
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx
from PIL import Image

from . import culling
from . import detect as detect_mod
from . import keywords as keywords_mod
from . import grouping
from . import store, vlm
from .analyze import VehicleAnalysis, analyze
from .config import CACHE_DIR, IMAGE_SUFFIXES, JPEG_SUFFIXES, RAW_SUFFIXES, Settings
from .exif import ExifTool, extract_previews
from .mapping import NumberMap
from .writer import write_keywords

Progress = Callable[[dict], None]


def _noop(_event: dict) -> None:
    pass


def scan(root: Path, recursive: bool = True) -> list[Path]:
    """Every frame under a folder, in a stable order."""
    walker = root.rglob("*") if recursive else root.glob("*")
    files = [p for p in walker if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
    files.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))
    return files


def _key_for(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


@dataclass
class JobSummary:
    job_id: int
    images: int
    detections: int
    identified: int


def run(root: Path, settings: Settings, *, label: str | None = None,
        recursive: bool = True, on_progress: Progress = _noop,
        should_stop: Callable[[], bool] | None = None,
        wait_if_paused: Callable[[], None] | None = None,
        resume_job: int | None = None) -> JobSummary:
    """Scan, detect and analyse a folder. Does not write metadata.

    ``resume_job`` continues a job that was stopped or interrupted: frames it
    already got through are skipped, so a 6,000-frame shoot that died an hour
    in does not start again from nothing.
    """
    # Progress reporting is a side channel: a failure in the UI's callback
    # must never take down a scan that is hours into a shoot.
    raw_progress = on_progress

    def on_progress(event: dict) -> None:      # noqa: F811 - deliberate shadow
        _safely(raw_progress, event)

    root = root.resolve()
    files = scan(root, recursive)
    on_progress({"stage": "scan", "done": len(files), "total": len(files),
                 "message": f"found {len(files)} frames"})
    if not files:
        raise SystemExit(f"No supported images under {root}")

    if settings.respect_culling:
        found = len(files)
        with ExifTool() as tool:
            files, skipped = culling.filter_frames(files, settings, tool)
        if skipped:
            detail = ", ".join(f"{n} {why}" for why, n in sorted(skipped.items()))
            on_progress({"stage": "cull", "done": len(files), "total": found,
                         "message": f"{len(files)} of {found} pass the cull "
                                    f"(skipped {detail})"})
        if not files:
            raise SystemExit(
                f"All {found} frames were filtered out by the culling rules."
            )

    if settings.use_vlm:
        vlm.check_available(settings)

    conn = store.connect()
    try:
        if resume_job is not None:
            job_id = resume_job
            store.set_job_status(conn, job_id, "scanning")
            store.add_images(conn, job_id, files)   # ignores ones already there
        else:
            job_id = store.create_job(conn, root, label, dict(settings.to_dict()))
            store.add_images(conn, job_id, files)

        # Frames already carrying a result are not worth doing twice.
        already = {Path(r["path"]) for r in conn.execute(
            "SELECT path FROM images WHERE job_id=? AND status='detected'",
            (job_id,)).fetchall()}
        if already:
            files = [f for f in files if f not in already]
            on_progress({"stage": "resume", "done": len(already),
                         "total": len(already) + len(files),
                         "message": f"resuming: {len(already)} frames already done, "
                                    f"{len(files)} to go"})
        if not files:
            store.set_job_status(conn, job_id, "analysed")
            conn.commit()
            return JobSummary(job_id, 0, 0, 0)

        previews = _prepare_previews(files, on_progress, settings)

        work: "queue.Queue" = queue.Queue(maxsize=64)
        errors: list[str] = []
        counters = {"analysed": 0, "identified": 0}
        counter_lock = threading.Lock()

        # A pool rather than one thread: plate detection and OCR are
        # onnxruntime calls that release the GIL and so genuinely overlap,
        # and keeping a request or two queued at Ollama stops the GPU idling
        # between frames.
        pool = [
            threading.Thread(
                target=_analysis_worker,
                args=(work, settings, counters, counter_lock, errors, on_progress),
                daemon=True,
            )
            for _ in range(max(1, settings.analysis_workers))
        ]
        for worker in pool:
            worker.start()

        rows = {Path(r["path"]): r["id"] for r in
                conn.execute("SELECT id, path FROM images WHERE job_id=?",
                             (job_id,)).fetchall()}

        total_detections = 0
        processed = 0
        for index, path in enumerate(files, start=1):
            if wait_if_paused:
                wait_if_paused()
            if should_stop and should_stop():
                on_progress({"stage": "stopped", "done": index, "total": len(files),
                             "message": "stopped by request"})
                break

            processed = index
            image_id = rows[path]
            is_jpeg = path.suffix.lower() in JPEG_SUFFIXES
            source = previews.get(path, path if is_jpeg else None)
            if source is None:
                store.set_image_result(conn, image_id, status="error",
                                       error="no preview could be extracted")
                continue
            on_progress({"stage": "frame", "done": index, "total": len(files),
                         "frame": {"name": path.name, "preview": str(source),
                                   "phase": "scanning", "boxes": []}})
            try:
                detections = detect_mod.detect(source, settings)
                if detections:
                    detect_mod.write_crops(source, detections, settings,
                                           _key_for(path))

                # Report the boxes as soon as they exist, in fractions of the
                # frame so the UI can draw them over any preview size.
                with Image.open(source) as probe:
                    frame_w, frame_h = probe.size
                boxes = []
                for det in detections:
                    if not det.crop_path:
                        continue
                    det_id = store.add_detection(
                        conn, image_id, det.box, det.cls, det.conf, str(det.crop_path)
                    )
                    total_detections += 1
                    x1, y1, x2, y2 = det.box
                    boxes.append({
                        "id": det_id, "kind": det.cls, "conf": round(det.conf, 3),
                        "x": x1 / frame_w, "y": y1 / frame_h,
                        "w": (x2 - x1) / frame_w, "h": (y2 - y1) / frame_h,
                    })
                    # Bounded put with a stop check rather than a blocking
                    # one, so a wedged analysis pool cannot hang the scan.
                    while True:
                        if should_stop and should_stop():
                            break
                        try:
                            work.put((det_id, det.crop_path, det.is_bike,
                                      det.cls, str(source), det.box),
                                     timeout=1.0)
                            break
                        except queue.Full:
                            continue

                on_progress({"stage": "frame", "done": index, "total": len(files),
                             "frame": {"name": path.name, "preview": str(source),
                                       "phase": "detected" if boxes else "empty",
                                       "boxes": boxes}})
                store.set_image_result(conn, image_id, status="detected",
                                       preview_path=str(source))
            except Exception as exc:  # one bad frame must not end the shoot
                store.set_image_result(conn, image_id, status="error",
                                       error=f"{type(exc).__name__}: {exc}")
                errors.append(f"{path.name}: {exc}")
            if index % 10 == 0 or index == len(files):
                conn.commit()
            on_progress({"stage": "detect", "done": index, "total": len(files),
                         "message": f"{total_detections} vehicles, "
                                    f"{counters['identified']} identified"})

        conn.commit()
        for _ in pool:
            while True:
                try:
                    work.put(None, timeout=1.0)
                    break
                except queue.Full:
                    if not any(w.is_alive() for w in pool):
                        break       # nothing left to tell
        for worker in pool:
            worker.join(timeout=300)

        if settings.group_vehicles:
            on_progress({"stage": "grouping", "done": 0, "total": 0,
                         "message": "grouping vehicles that look the same"})
            try:
                stats = grouping.consolidate(conn, job_id, settings)
                on_progress({"stage": "grouping", "done": stats["vehicles"],
                             "total": stats["vehicles"],
                             "message": f"{stats['vehicles']} vehicles in "
                                        f"{stats['groups']} groups"})
            except Exception as exc:
                errors.append(f"grouping: {exc}")

        store.set_job_status(conn, job_id, "analysed")
        conn.commit()

        if errors:
            on_progress({"stage": "warn", "done": 0, "total": 0,
                         "message": f"{len(errors)} frames failed; first: {errors[0]}"})

        return JobSummary(job_id, processed, total_detections,
                          counters["identified"])
    finally:
        conn.close()


def _prepare_previews(files: Iterable[Path], on_progress: Progress,
                      settings: Settings) -> dict[Path, Path]:
    """Extract the embedded JPEG from every RAW, reporting as it goes.

    This is the longest silent stretch of a big shoot -- thousands of
    full-resolution JPEGs written out before a single vehicle is detected --
    so it reports every batch rather than only at the end.
    """
    raws = [f for f in files if f.suffix.lower() in RAW_SUFFIXES]
    if not raws:
        return {}

    started = time.monotonic()

    def report(done: int, total: int) -> None:
        rate = done / max(time.monotonic() - started, 0.001)
        left = (total - done) / rate if rate else 0
        on_progress({"stage": "preview", "done": done, "total": total,
                     "message": f"extracting previews {done}/{total}"
                                + (f" · about {_mmss(left)} left" if done > 8 else "")})

    on_progress({"stage": "preview", "done": 0, "total": len(raws),
                 "message": f"extracting previews from {len(raws)} RAW files"})
    previews = extract_previews(raws, CACHE_DIR / "previews",
                                on_progress=report,
                                workers=max(1, settings.preview_workers))
    on_progress({"stage": "preview", "done": len(previews), "total": len(raws),
                 "message": f"extracted {len(previews)}/{len(raws)} previews"})
    return previews


def _mmss(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def _native_region(frame_path: str, box, settings: Settings):
    """The vehicle at full resolution, for the plate search.

    Read here rather than passed through the queue: the queue is 64 deep and
    one of these is about 11 MB, which would be most of a gigabyte in flight.
    """
    if not settings.plate_native_search or not settings.read_plates:
        return None
    try:
        with Image.open(frame_path) as frame:
            frame.load()
            x1, y1, x2, y2 = (int(v) for v in box)
            return frame.crop((x1, y1, x2, y2))
    except Exception:
        return None


def _safely(fn, *args) -> None:
    """Call a callback that must never take the caller down with it."""
    try:
        fn(*args)
    except Exception:
        pass


def _analysis_worker(work, settings: Settings, counters: dict,
                     counter_lock: threading.Lock, errors: list[str],
                     on_progress: Progress) -> None:
    """Drain crops off the queue, analyse them, and record the results.

    Each worker owns its own SQLite connection and HTTP client; sharing either
    across threads is what turns a fast pipeline into a flaky one.
    """
    conn = store.connect()
    client = httpx.Client(timeout=settings.vlm_timeout)
    try:
        pending = 0
        while True:
            item = work.get()
            if item is None:
                break
            det_id, crop_path, is_bike, kind, frame_path, box = item
            try:
                native = _native_region(frame_path, box, settings)
                with Image.open(crop_path) as crop:
                    crop.load()
                    analysis = analyze(crop, settings, is_bike=is_bike, kind=kind,
                                       client=client, native=native)
                store.set_analysis(conn, det_id, analysis)
                identified = bool(analysis.race_number or analysis.plate
                                  or analysis.make)
            except Exception as exc:
                errors.append(f"analyse {crop_path}: {exc}")
                analysis = VehicleAnalysis()
                try:
                    store.set_analysis(conn, det_id, analysis)
                except Exception:
                    pass
                identified = False

            with counter_lock:
                counters["analysed"] += 1
                if identified:
                    counters["identified"] += 1
                done, hits = counters["analysed"], counters["identified"]

            on_progress({
                "stage": "vehicle", "done": done, "total": 0,
                "message": f"{hits} identified",
                "vehicle": {
                    "id": det_id,
                    "number": analysis.race_number,
                    "plate": analysis.plate,
                    "title": analysis.title,
                    "team": analysis.team if analysis.team_corroborated else None,
                    "conf": round(max(analysis.number_conf, analysis.vlm_conf), 3),
                },
            })

            pending += 1
            if pending >= 5:
                conn.commit()
                pending = 0
        conn.commit()
    finally:
        client.close()
        conn.close()


WRITE_QUERY = """
SELECT i.id AS image_id, i.path AS path, d.attributes AS attributes
  FROM images i
  JOIN detections d ON d.image_id = i.id
 WHERE i.job_id = ?
   AND d.rejected = 0
   AND d.attributes IS NOT NULL
 ORDER BY i.id, d.id
"""


def write_job(job_id: int, settings: Settings, number_map: NumberMap | None = None,
              *, dry_run: bool = False, on_progress: Progress = _noop) -> dict:
    """Push everything read off each frame's vehicles into XMP."""
    conn = store.connect()
    try:
        by_image: dict[int, tuple[Path, list[VehicleAnalysis]]] = {}
        for row in conn.execute(WRITE_QUERY, (job_id,)).fetchall():
            entry = by_image.setdefault(row["image_id"], (Path(row["path"]), []))
            entry[1].append(VehicleAnalysis.from_json(row["attributes"]))

        written = failed = skipped = 0
        with ExifTool() as tool:
            for index, (image_id, (path, analyses)) in enumerate(
                    by_image.items(), start=1):
                words = keywords_mod.for_frame(analyses, settings, number_map)
                if not words:
                    skipped += 1
                    continue
                if dry_run:
                    on_progress({"stage": "write", "done": index,
                                 "total": len(by_image),
                                 "message": f"{path.name}: {', '.join(words)}"})
                    continue
                caption = (keywords_mod.caption_for(analyses)
                           if settings.write_caption else None)
                result = write_keywords(tool, path, words, settings,
                                        caption=caption)
                if result.ok:
                    written += 1
                    conn.execute("UPDATE images SET written_at=? WHERE id=?",
                                 (time.time(), image_id))
                else:
                    failed += 1
                on_progress({"stage": "write", "done": index, "total": len(by_image),
                             "message": path.name})
            conn.commit()

        if not dry_run:
            store.set_job_status(conn, job_id, "written")
        return {"frames": len(by_image), "written": written, "failed": failed,
                "skipped": skipped, "dry_run": dry_run}
    finally:
        conn.close()
