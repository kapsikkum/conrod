"""The orchestrator: folder in, keyworded frames out.

Stages run overlapped where it pays. Detection is CPU-bound and the vision
model is GPU-bound, so analysis runs on its own thread and consumes crops as
detection produces them rather than waiting for the whole shoot to be scanned.
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx
from PIL import Image

from . import bursts
from . import colour as colour_mod
from . import framing
from . import culling
from . import detect as detect_mod
from . import keywords as keywords_mod
from . import grouping
from . import sharpness as sharpness_mod
from . import store, vlm, vlm_providers
from .analyze import VehicleAnalysis, analyze
from .config import (BIKE_CLASS_NAMES, CACHE_DIR, IMAGE_SUFFIXES, JPEG_SUFFIXES,
                     RAW_SUFFIXES, Settings)
from .exif import ExifTool, extract_previews
from .mapping import NumberMap
from .writer import write_keywords

Progress = Callable[[dict], None]

# How many newly-identified vehicles between re-groupings while a scan is
# still running. consolidate() is a full pass over the job's own detections,
# not an incremental one, so this is a balance: too low and a big album
# spends its GPU time re-grouping instead of identifying; too high and a
# long scan looks like nothing has been grouped at all until it finishes,
# which read as "grouping is an afterthought" on a multi-thousand-frame
# album. It always still runs once more at the very end regardless.
REGROUP_CHECKPOINT = 250

# What a file already says about itself. Both are what Lightroom and Bridge
# read and write, so a shoot that has been culled once by hand arrives with
# the photographer's own opinion attached and the cull can defer to it.
EXISTING_MARK_TAGS = ("Rating", "Label")


def _maybe_regroup(conn, job_id: int, settings: Settings, counters: dict,
                   counter_lock: threading.Lock, checkpoint: dict,
                   errors: list[str], on_progress: Progress) -> None:
    """Group what has been identified so far, at most every so often.

    Grouping mid-scan is what turns a wall of ungrouped single-frame cards
    into vehicles the moment there is enough evidence to name one, instead
    of only once the last frame of the album has been read. Safe to call
    this often -- consolidate() works entirely from stored crops and is
    documented as safe to re-run any number of times -- so this is purely a
    cost trade-off, not a correctness one.
    """
    if not settings.group_vehicles:
        return
    with counter_lock:
        analysed = counters["analysed"]
    if analysed - checkpoint["last"] < REGROUP_CHECKPOINT:
        return
    checkpoint["last"] = analysed
    try:
        stats = grouping.consolidate(conn, job_id, settings)
        on_progress({"stage": "grouping", "done": stats["vehicles"],
                     "total": stats["vehicles"],
                     "message": f"{stats['vehicles']} vehicles in "
                                f"{stats['groups']} groups so far"})
    except Exception as exc:
        errors.append(f"grouping: {exc}")


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
        resume_job: int | None = None,
        stop_after: str | None = None,
        cull: bool = True) -> JobSummary:
    """Scan, detect and analyse a folder. Does not write metadata.

    ``resume_job`` continues a job that was stopped or interrupted: frames it
    already got through are skipped, so a 6,000-frame shoot that died an hour
    in does not start again from nothing.

    ``stop_after`` runs part of the work and stops, so adding an album does
    not commit anyone to hours of GPU time:

      "index"  find the frames, read their EXIF, extract previews. Minutes,
               and enough to browse the album and see what is in it.
      "cull"   also detect the vehicles and rate them, dropping the ones too
               blurred or too far outside the frame to be worth naming. No
               vision model runs, so this is roughly a second a frame.
      None     everything, with identification overlapping detection. This
               is the fast path and stays the default.

    Identification of an album culled this way is a separate call --
    ``identify`` below -- which works from the stored crops.
    """
    if stop_after not in (None, "index", "cull"):
        raise ValueError(f"unknown stage {stop_after!r}")
    analyse = stop_after is None

    # Culling is a separate question from finding and naming the cars, and
    # the answer is allowed to be "no". Identifying an album nobody has culled
    # is a real way to work -- keep everything, name everything, decide later
    # -- and it used to be unreachable, because detections only ever came
    # into existence during a cull. Everything is still measured and rated
    # either way; this only decides whether a rating is acted on.
    if stop_after == "cull" and not cull:
        raise ValueError("a cull that culls nothing is just detection")
    # Progress reporting is a side channel: a failure in the UI's callback
    # must never take down a scan that is hours into a shoot.
    raw_progress = on_progress

    def on_progress(event: dict) -> None:      # noqa: F811 - deliberate shadow
        _safely(raw_progress, event)

    # Let the provider's waits notice a Stop. A rate limit is waited out
    # rather than skipped, so without this a scan stopped mid-limit would sit
    # there until the provider relented before noticing it had been stopped.
    vlm_providers.set_stop_check(should_stop or (lambda: False))

    root = root.resolve()
    files = scan(root, recursive)
    on_progress({"stage": "scan", "done": len(files), "total": len(files),
                 "message": f"found {len(files)} frames"})
    if not files:
        raise SystemExit(f"No supported images under {root}")

    if settings.respect_culling:
        found = len(files)
        on_progress({"stage": "cull", "done": 0, "total": found,
                     "message": f"checking ratings and labels on {found} frames"})

        def cull_progress(done: int, total: int) -> None:
            on_progress({"stage": "cull", "done": done, "total": total,
                         "message": f"checked {done}/{total} frames"})

        with ExifTool() as tool:
            files, skipped = culling.filter_frames(files, settings, tool,
                                                   cull_progress)
        if skipped:
            detail = ", ".join(f"{n} {why}" for why, n in sorted(skipped.items()))
            on_progress({"stage": "cull", "done": len(files), "total": found,
                         "message": f"{len(files)} of {found} pass the cull "
                                    f"(skipped {detail})"})
        if not files:
            raise SystemExit(
                f"All {found} frames were filtered out by the culling rules."
            )

    if settings.use_vlm and analyse:
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

        _record_origins(conn, job_id, files, on_progress, settings,
                        should_stop)

        previews = _prepare_previews(files, on_progress, settings)

        if stop_after == "index":
            # The album exists, its frames are known and every one of them has
            # a preview to show. Nothing has looked for a vehicle yet, which
            # is the point: adding a folder should cost minutes, not a night.
            store.set_job_status(conn, job_id, "indexed")
            conn.commit()
            on_progress({"stage": "indexed", "done": len(files),
                         "total": len(files),
                         "message": f"{len(files)} frames ready to cull"})
            return JobSummary(job_id, len(files), 0, 0)

        work: "queue.Queue" = queue.Queue(maxsize=64)
        errors: list[str] = []
        counters = {"analysed": 0, "identified": 0}
        counter_lock = threading.Lock()
        checkpoint = {"last": 0}

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
        ] if analyse else []
        for worker in pool:
            worker.start()

        rows = {Path(r["path"]): r["id"] for r in
                conn.execute("SELECT id, path FROM images WHERE job_id=?",
                             (job_id,)).fetchall()}

        total_detections = 0
        culled = 0
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
                    box = {
                        "id": det_id, "kind": det.cls, "conf": round(det.conf, 3),
                        "x": x1 / frame_w, "y": y1 / frame_h,
                        "w": (x2 - x1) / frame_w, "h": (y2 - y1) / frame_h,
                    }
                    boxes.append(box)

                    # Judge the crop before spending anything on it.
                    # Sharpness is about sixteen milliseconds and the frame
                    # edge is arithmetic; the vision model is seconds, and no
                    # amount of it will name a car that is not there to be
                    # seen or is half outside the picture.
                    focus = _focus_of(det.crop_path, settings,
                                      box=det.box, crop_box=det.crop_box)
                    edges = framing.assess(det.box, frame_w, frame_h)
                    if focus:
                        rating = focus.score * edges.factor
                        verdict = sharpness_mod.rating_for(
                            rating, settings.sharp_at, settings.blurred_below)
                        unsure = sharpness_mod.doubtful(
                            focus, verdict, settings.blurred_below)
                        store.set_quality(
                            conn, det_id, sharpness=focus.score,
                            sharpness_verdict=focus.verdict, clipped=edges.sides,
                            rating=rating, rating_verdict=verdict,
                            panning=focus.panning, sharp_end=focus.sharp_end,
                            background=focus.background, uncertain=unsure)
                        box["focus"] = focus.verdict
                        box["rating"] = verdict
                        # The stars the cull would write. Carried on the box
                        # so the live view can say what it decided without
                        # keeping its own copy of the bands.
                        stars = sharpness_mod.stars_for(rating)
                        box["stars"] = stars
                        if focus.panning:
                            box["panning"] = True

                        # The photographer's own standing instruction, on the
                        # star the cull just worked out. Deliberately ahead of
                        # the blur cull and deliberately without the pan
                        # exemption: that exemption protects a held pan from
                        # the machine's opinion, and this is not the machine's
                        # opinion -- it is a rule someone set knowing the
                        # whole scale. A pan that lands on one star has a
                        # smeared subject, which is the thing being asked
                        # about. Rejected, not deleted, so the Rejected view
                        # puts any of it back.
                        floor = settings.auto_reject_below_stars if cull else 0
                        if floor and stars < floor:
                            store.cull_detection(
                                conn, det_id,
                                f"{stars} star{'' if stars == 1 else 's'}"
                                f", below your limit of {floor}",
                                uncertain=unsure)
                            culled += 1
                            box["culled"] = True
                            continue

                        # A held pan is never culled automatically, and a
                        # close call is culled but flagged, so it turns up in
                        # review instead of vanishing.
                        if (cull and settings.cull_blurred
                                and sharpness_mod.cullable(focus, verdict)):
                            store.cull_detection(
                                conn, det_id, _cull_reason(focus, edges),
                                uncertain=unsure)
                            culled += 1
                            box["culled"] = True
                            continue

                    # A cull-only pass stops here: the crop is written, rated
                    # and kept, and naming it is a separate decision made
                    # later against a much smaller set of frames.
                    if not analyse:
                        continue

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
                if analyse:
                    _maybe_regroup(conn, job_id, settings, counters,
                                   counter_lock, checkpoint, errors, on_progress)
            note = (f"{total_detections} vehicles, "
                    f"{counters['identified']} identified")
            if culled:
                note += f", {culled} culled"
            on_progress({"stage": "detect", "done": index, "total": len(files),
                         "message": note})

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

        if not analyse:
            # Detected, rated and culled. Grouping is deliberately not run:
            # it settles on a name, and nothing has proposed one yet.
            store.set_job_status(conn, job_id, "culled")
            conn.commit()
            kept = total_detections - culled
            on_progress({"stage": "culled", "done": processed, "total": len(files),
                         "message": f"{kept} vehicles kept, {culled} culled"})
            return JobSummary(job_id, processed, total_detections, 0)

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


def rescore(job_id: int | None, settings: Settings, *,
            on_progress: Progress = _noop,
            should_stop: Callable[[], bool] | None = None) -> int:
    """Measure every stored crop again and write the new sharpness back.

    Needed whenever the focus scale is re-derived. A rating is a number about
    a scale: the album on disk was scored against the old one, and reading it
    with the new bands is not a stale answer but a wrong one -- the shoot
    these bands were fitted to tops out at 0.711 on the old scale, and the
    new three-star floor is 0.728, so every frame in it would read as one
    star until it was measured again.

    Works entirely from the stored crops, like identify does, so it re-reads
    no photographs and takes minutes rather than the hours a fresh scan
    would. Ratings given by hand are not touched: they are in a different
    column and they were never on this scale to begin with.
    """
    where = "WHERE i.job_id = ?" if job_id is not None else ""
    args = (job_id,) if job_id is not None else ()
    conn = store.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            f"""SELECT d.id, d.crop_path, d.x1, d.y1, d.x2, d.y2
                  FROM detections d JOIN images i ON i.id = d.image_id
                 {where} {'AND' if where else 'WHERE'} d.crop_path IS NOT NULL""",
            args)]
        total = len(rows)
        on_progress({"stage": "rescore", "done": 0, "total": total,
                     "message": f"re-measuring {total} crops"})
        done = 0
        for row in rows:
            if should_stop and should_stop():
                break
            width = row["x2"] - row["x1"]
            height = row["y2"] - row["y1"]
            pad = settings.crop_padding
            crop_box = (row["x1"] - width * pad, row["y1"] - height * pad,
                        row["x2"] + width * pad, row["y2"] + height * pad)
            focus = _focus_of(row["crop_path"], settings,
                              box=(row["x1"], row["y1"], row["x2"], row["y2"]),
                              crop_box=crop_box)
            done += 1
            if not focus:
                continue
            # The framing factor is not recoverable here -- it needs the frame
            # size, which the crop does not carry -- so the stored ratio
            # between rating and sharpness is reused. On a frame that was
            # never clipped that ratio is one, which is nearly all of them.
            old = conn.execute(
                "SELECT sharpness, rating FROM detections WHERE id=?",
                (row["id"],)).fetchone()
            factor = 1.0
            if old and old["sharpness"] and old["rating"] is not None:
                factor = min(1.0, old["rating"] / old["sharpness"])
            rating = focus.score * factor
            verdict = sharpness_mod.rating_for(
                rating, settings.sharp_at, settings.blurred_below)
            conn.execute(
                """UPDATE detections SET sharpness=?, sharpness_verdict=?,
                       rating=?, rating_verdict=?, panning=?, background=?,
                       sharp_end=? WHERE id=?""",
                (focus.score, focus.verdict, rating, verdict,
                 int(bool(focus.panning)), focus.background, focus.sharp_end,
                 row["id"]))
            if done % 100 == 0:
                conn.commit()
                on_progress({"stage": "rescore", "done": done, "total": total,
                             "message": f"re-measured {done}/{total}"})
        conn.commit()
        on_progress({"stage": "rescore", "done": done, "total": total,
                     "message": f"re-measured {done} crops"})
        return done
    finally:
        conn.close()


def identify(job_id: int, settings: Settings, *, on_progress: Progress = _noop,
             should_stop: Callable[[], bool] | None = None,
             wait_if_paused: Callable[[], None] | None = None) -> JobSummary:
    """Name the vehicles of an album that has already been detected and culled.

    The counterpart to ``run(stop_after="cull")``. It works entirely from the
    stored crops and boxes, so it re-reads no photographs, and it only looks
    at detections that survived the cull and have not been named yet -- which
    is what makes culling first worth doing. On a real shoot that is a third
    fewer calls to the vision model, each of them seconds long.

    Safe to run twice: the second pass finds nothing left to do.
    """
    raw_progress = on_progress

    def on_progress(event: dict) -> None:      # noqa: F811 - deliberate shadow
        _safely(raw_progress, event)

    # As in run(): a rate limit is waited out, so the wait has to be able to
    # hear a Stop.
    vlm_providers.set_stop_check(should_stop or (lambda: False))

    if settings.use_vlm:
        vlm.check_available(settings)

    conn = store.connect()
    try:
        store.set_job_status(conn, job_id, "scanning")
        pending = conn.execute(
            """SELECT d.id, d.crop_path, d.cls, d.x1, d.y1, d.x2, d.y2,
                      COALESCE(i.preview_path, i.path) AS frame
                 FROM detections d JOIN images i ON i.id = d.image_id
                WHERE i.job_id = ? AND d.rejected = 0 AND d.crop_path IS NOT NULL
                  AND COALESCE(d.bystander, 0) = 0
                  AND (d.attributes IS NULL OR d.attributes = '')
                ORDER BY d.id""",
            (job_id,),
        ).fetchall()
        total = len(pending)
        on_progress({"stage": "identify", "done": 0, "total": total,
                     "message": f"{total} vehicles to identify"})
        if not total:
            store.set_job_status(conn, job_id, "analysed")
            conn.commit()
            return JobSummary(job_id, 0, 0, 0)

        work: "queue.Queue" = queue.Queue(maxsize=64)
        errors: list[str] = []
        counters = {"analysed": 0, "identified": 0}
        counter_lock = threading.Lock()
        checkpoint = {"last": 0}
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

        for done, row in enumerate(pending, start=1):
            if wait_if_paused:
                wait_if_paused()
            if should_stop and should_stop():
                on_progress({"stage": "stopped", "done": done, "total": total,
                             "message": "stopped by request"})
                break
            box = (row["x1"], row["y1"], row["x2"], row["y2"])
            # A bike is judged by the detector's class, which is what the
            # detect loop passed too -- the reader asks a different question
            # about a rider than about a car.
            is_bike = (row["cls"] or "").lower() in BIKE_CLASS_NAMES

            # Tell the live view this frame exists. It only ever learned
            # about frames from the detect loop, which this stage does not
            # run -- so identifying an album that had been culled earlier
            # showed "Reading the folder..." and an empty panel for the whole
            # job, however many hours it took. The size comes from the JPEG
            # header, not from decoding it.
            _announce_frame(row["frame"], row["id"], box, row["cls"],
                            on_progress, done, total)
            while True:
                if should_stop and should_stop():
                    break
                try:
                    work.put((row["id"], Path(row["crop_path"]), is_bike,
                              row["cls"], row["frame"], box), timeout=1.0)
                    break
                except queue.Full:
                    continue
            # Progress is how many have been *read*, not how many have been
            # handed over. The queue is 64 deep, so a small album is fully
            # queued in milliseconds: reporting the hand-over sent the bar
            # straight to 100% and then showed "0 named" for the nine minutes
            # the work actually took.
            with counter_lock:
                seen = counters["analysed"]
                named = counters["identified"]
            on_progress({"stage": "identify", "done": seen, "total": total,
                         "message": f"{named} named"})

        for _ in pool:
            while True:
                try:
                    work.put(None, timeout=1.0)
                    break
                except queue.Full:
                    if not any(w.is_alive() for w in pool):
                        break

        # Everything is queued and the workers are still going. Keep
        # reporting until they stop, or the last frame of the album appears
        # to take as long as all of them put together.
        while any(w.is_alive() for w in pool):
            for worker in pool:
                worker.join(timeout=1.0)
                break
            with counter_lock:
                seen = counters["analysed"]
                named = counters["identified"]
            on_progress({"stage": "identify", "done": min(seen, total),
                         "total": total, "message": f"{named} named"})
            _maybe_regroup(conn, job_id, settings, counters, counter_lock,
                          checkpoint, errors, on_progress)
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
                         "message": f"{len(errors)} failed; first: {errors[0]}"})
        return JobSummary(job_id, 0, total, counters["identified"])
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


def _announce_frame(frame_path: str, det_id: int, box, cls: str | None,
                    on_progress: Progress, done: int, total: int) -> None:
    """Put a frame the identify stage is about to read on the live view.

    The view is keyed by detection id and filled by the detect loop, which
    only runs when detection runs. Identifying an already-culled album
    therefore had nothing to show: an empty panel reading "Reading the
    folder..." for the whole job.

    Only the header is read, so this costs a stat and a few hundred bytes
    rather than decoding a 32-megapixel preview per vehicle.
    """
    try:
        with Image.open(frame_path) as frame:
            width, height = frame.size
    except Exception:
        return
    if not width or not height:
        return
    x1, y1, x2, y2 = (float(v) for v in box)
    on_progress({"stage": "frame", "done": done, "total": total,
                 "frame": {"name": Path(frame_path).name,
                           "preview": str(frame_path),
                           "phase": "identifying",
                           "boxes": [{"id": det_id, "kind": cls or "vehicle",
                                      "conf": 0.0,
                                      "x": x1 / width, "y": y1 / height,
                                      "w": (x2 - x1) / width,
                                      "h": (y2 - y1) / height}]}})


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


def _cull_reason(focus, edges) -> str:
    """Say which fault cut it, because the two are fixed differently.

    "Too blurred" on a pin-sharp frame that ran off the edge would send
    someone hunting a focus problem that was never there.
    """
    if edges.cut_off and focus.verdict != "blurred":
        return framing.describe(edges)
    if edges.cut_off:
        return f"blurred, and {framing.describe(edges)}"
    # Where the softness sits is the difference between a missed frame and a
    # pan that was held on the wrong end of the car, and only one of those is
    # worth a second look.
    if focus.partly_sharp and focus.sharp_end != "even":
        return f"soft overall, though the {focus.sharp_end} is sharper"
    return "too blurred to identify"


def _focus_of(crop_path, settings, box=None, crop_box=None):
    """Subject sharpness for one crop, or nothing if it cannot be judged.

    ``box`` and ``crop_box`` are the vehicle and the padded region the crop
    was cut from, both in frame coordinates. Given them, sharpness is measured
    on the vehicle rather than on the whole crop -- which is what stops a
    smeared background from condemning a good pan, and a sharp fence behind a
    soft car from rescuing a bad frame.

    Never worth failing a detection over: an unreadable crop is a detection
    with no score, not a scan that stops.
    """
    try:
        with Image.open(crop_path) as crop:
            crop.load()
            inner = None
            if box is not None and crop_box is not None:
                cx1, cy1, cx2, cy2 = crop_box
                span = cx2 - cx1
                if span > 0:
                    # The crop may have been resized on the way to disk, so
                    # the box is scaled by what actually came back.
                    scale = crop.width / span
                    inner = ((box[0] - cx1) * scale, (box[1] - cy1) * scale,
                             (box[2] - cx1) * scale, (box[3] - cy1) * scale)
            return sharpness_mod.rate(crop, settings, box=inner)
    except Exception:
        return None


def _record_origins(conn, job_id: int, files, on_progress, settings,
                    should_stop=None) -> None:
    """Work out which camera took each frame and which burst it belongs to.

    One exiftool pass over the whole shoot, before anything is analysed. Two
    shooters interleave into one folder and neither the filenames nor the
    timestamps separate them on their own, so this is what makes "the same
    run of frames" mean anything at all -- see bursts.py.

    Advisory, not required: a folder of files with no EXIF at all still
    scans, it just has one camera and a burst per frame.
    """
    files = list(files)
    want_bursts = bool(getattr(settings, "group_by_burst", True))
    want_marks = bool(getattr(settings, "import_existing_ratings", True))

    # Both of these are properties of the files, not of the run, so a second
    # pass over an album that already has them is an exiftool walk of the
    # whole shoot for an answer that cannot have changed. Adding an album and
    # then culling it is two passes by definition, and on 1,800 frames the
    # second one is minutes of nothing.
    if want_bursts:
        have = conn.execute(
            "SELECT COUNT(*) FROM images WHERE job_id=? AND burst_key IS NOT NULL",
            (job_id,)).fetchone()[0]
        if have and have >= len(files):
            want_bursts = False
            on_progress({"stage": "cameras",
                         "message": f"capture times already read for {have} frames"})
    if want_marks:
        seen = conn.execute(
            "SELECT COUNT(*) FROM images WHERE job_id=? AND rating_in_file IS NOT NULL",
            (job_id,)).fetchone()[0]
        if seen:
            want_marks = False
    if not (want_bursts or want_marks):
        return

    # One walk for both. They want different tags but the expensive part is
    # opening seventeen hundred RAW files, not which fields are asked for.
    wanted = list(bursts.TAGS) if want_bursts else []
    if want_marks:
        wanted += [tag for tag in EXISTING_MARK_TAGS if tag not in wanted]

    on_progress({"stage": "cameras",
                 "message": "reading camera, capture times and existing ratings"
                            if want_marks else "reading camera and capture times"})
    # Read in chunks and say so. One call for the whole shoot is faster in
    # theory and unusable in practice: 6,000 frames is minutes of opening
    # RAW files during which the screen showed a full progress bar, "about
    # 0s left" and no sign of life. culling.read_culls already chunks for
    # exactly this reason; this pass did not, and neither did the ratings
    # pass that was added beside it.
    total = len(files)
    rows = []
    try:
        with ExifTool() as tool:
            for start in range(0, total, culling.CULL_CHUNK):
                if should_stop and should_stop():
                    return
                batch = files[start:start + culling.CULL_CHUNK]
                rows += tool.read_tags(batch, wanted)
                done = min(start + culling.CULL_CHUNK, total)
                on_progress({"stage": "cameras", "done": done, "total": total,
                             "message": f"read {done}/{total} frames"})
    except Exception as exc:                      # exiftool missing or unhappy
        on_progress({"stage": "cameras",
                     "message": f"could not read capture times: {exc}"})
        return

    said = []
    if want_bursts:
        frames = bursts.describe(
            rows, fallback="camera",
            gap=getattr(settings, "burst_gap", bursts.BURST_GAP_SECONDS))
        store.set_frame_origin(conn, job_id, frames)
        cameras = {f.camera for f in frames}
        runs = len({f.burst for f in frames})
        said.append(f"{runs} burst{'' if runs == 1 else 's'} from "
                    f"{len(cameras)} camera{'' if len(cameras) == 1 else 's'}")

    if want_marks:
        marks = _existing_marks(files, rows, on_progress, should_stop)
        rated = store.set_existing_marks(conn, job_id, marks)
        if rated:
            said.append(f"{rated} frame{'' if rated == 1 else 's'} already rated")

    if said:
        on_progress({"stage": "cameras", "message": ", ".join(said)})


def _existing_marks(files, rows, on_progress=_noop, should_stop=None) -> dict:
    """The rating and label each frame already carries, sidecar first.

    A RAW is not where Lightroom puts a rating -- it writes an .xmp beside
    the file and leaves the original untouched. Measured on one shoot: all
    1,833 CR3s reported 0 while their sidecars held 310 fives and nine ones,
    so a reader that asks only the RAW finds nothing on a shoot that has been
    rated for weeks.

    ``rows`` is what the burst pass already read off the images themselves,
    and the mark tags were asked for in that same call -- so a frame with no
    sidecar is already answered and does not need opening twice. Only the
    files that actually have a sidecar are read again, through
    culling.read_culls, which knows the sidecar rule and the XMP:Rating
    fallback. On a shoot straight off the card that is no second pass at all;
    on one that has been through Lightroom it is a pass over small text files
    rather than over six thousand RAWs.
    """
    marks = {}
    for row in rows:                          # what the images said themselves
        source = row.get("SourceFile")
        if source:
            marks[os.path.normcase(os.path.normpath(source))] = (
                row.get("Rating"), row.get("Label"))

    by_key = {os.path.normcase(os.path.normpath(str(f))): f for f in files}
    out = {}
    for key, path in by_key.items():
        rating, label = marks.get(key, (None, None))
        out[str(path)] = (rating, label)

    sidecars = [f for f in files if culling.sidecar_for(Path(f)).exists()]
    if not sidecars:
        return out

    def tick(done: int, of: int) -> None:
        on_progress({"stage": "cameras", "done": done, "total": of,
                     "message": f"checked {done}/{of} sidecars for ratings"})

    try:
        with ExifTool() as tool:
            found = culling.read_culls(sidecars, tool, tick)
    except Exception:            # worth having, not worth failing a scan over
        return out
    for path, cull in found.items():
        out[str(path)] = (cull.rating, cull.label)
    return out


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
                    # Sampled from the crop rather than taken from the model's
                    # word for it, so the review grid can show the paint.
                    # Never worth failing a whole detection over.
                    try:
                        swatch = colour_mod.dominant(crop)
                    except Exception:
                        swatch = None
                store.set_analysis(conn, det_id, analysis, colour_hex=swatch)
                identified = bool(analysis.race_number or analysis.plate
                                  or analysis.make)
            except vlm_providers.Stopped:
                # Stop, pressed while a rate limit was being waited out. Put
                # the crop back so a resume picks it up rather than counting
                # it as analysed-and-empty, and leave the queue for the other
                # workers to drain on their own way out.
                break
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

            # Connections autocommit (see store.connect), so there is
            # nothing to batch and nothing held open across the next
            # model call. Left explicit rather than silently removed.
            conn.commit()
        conn.commit()
    finally:
        client.close()
        conn.close()


# Every vehicle of the job, culled ones included. The cull's verdict is a
# rating and a colour label, and a frame that was culled is exactly the frame
# that needs to arrive in the catalogue marked red -- so it cannot be filtered
# out of the write the way it used to be.
WRITE_QUERY = """
SELECT i.id AS image_id, i.path AS path, d.attributes AS attributes,
       d.rejected AS rejected, d.rating AS rating,
       d.rating_verdict AS rating_verdict, d.panning AS panning,
       d.stars AS stars, d.bystander AS bystander
  FROM images i
  JOIN detections d ON d.image_id = i.id
 WHERE i.job_id = ?
 ORDER BY i.id, d.id
"""


def write_job(job_id: int, settings: Settings, number_map: NumberMap | None = None,
              *, dry_run: bool = False, on_progress: Progress = _noop) -> dict:
    """Push everything read off each frame's vehicles into XMP."""
    conn = store.connect()
    try:
        by_image: dict[int, dict] = {}
        for row in conn.execute(WRITE_QUERY, (job_id,)).fetchall():
            entry = by_image.setdefault(
                row["image_id"], {"path": Path(row["path"]), "analyses": [],
                                  "best": None, "panning": False,
                                  "by_hand": None})
            # A vehicle in the frame that is not what the frame is of says
            # nothing about it: not its keywords, not its rating, not the
            # stars. Rejecting is a judgement about the photograph; this is
            # a statement about which car in it is the subject.
            if row["bystander"]:
                continue
            # Keywords come only from vehicles that survived and were read.
            if not row["rejected"] and row["attributes"]:
                entry["analyses"].append(
                    VehicleAnalysis.from_json(row["attributes"]))
            # The rating is the best vehicle in the frame: a photograph is
            # kept for its best subject, not marked down for a blurred car
            # that happened to be in the background of a good one.
            if row["rating"] is not None:
                if entry["best"] is None or row["rating"] > entry["best"]:
                    entry["best"] = row["rating"]
            # A rating given by hand in review is the answer, not a proposal.
            # Same rule -- the frame takes its best vehicle -- but a starred
            # vehicle beats any measured one, so a frame the photographer
            # picked out cannot be written down by the sharpness measure.
            if row["stars"] is not None:
                if entry["by_hand"] is None or row["stars"] > entry["by_hand"]:
                    entry["by_hand"] = row["stars"]
            entry["panning"] = entry["panning"] or bool(row["panning"])

        written = failed = skipped = 0
        with ExifTool() as tool:
            for index, (image_id, entry) in enumerate(by_image.items(), start=1):
                path, analyses = entry["path"], entry["analyses"]
                words = keywords_mod.for_frame(analyses, settings, number_map)
                rating = label = None
                if entry["best"] is not None:
                    verdict = sharpness_mod.rating_for(
                        entry["best"], settings.sharp_at, settings.blurred_below)
                    rating = sharpness_mod.stars_for(entry["best"])
                    label = sharpness_mod.label_for(verdict)
                if entry["by_hand"] is not None:
                    rating = entry["by_hand"]
                    # The colour follows the stars they gave, so a frame
                    # starred in review does not stay red in the catalogue.
                    label = sharpness_mod.label_for(
                        "good" if rating >= 3 else
                        "fair" if rating == 2 else "poor")
                if not words and rating is None:
                    skipped += 1
                    continue
                if dry_run:
                    said = ", ".join(words) if words else "(no keywords)"
                    if label:
                        # Spelled out rather than drawn. This line reaches a
                        # console, and a Windows console is cp1252: a star
                        # glyph raises UnicodeEncodeError and takes the whole
                        # write down with it.
                        said += f"  [{label}, {rating} stars]"
                    on_progress({"stage": "write", "done": index,
                                 "total": len(by_image),
                                 "message": f"{path.name}: {said}"})
                    continue
                caption = (keywords_mod.caption_for(analyses)
                           if settings.write_caption else None)
                result = write_keywords(tool, path, words, settings,
                                        caption=caption, rating=rating,
                                        label=label)
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
