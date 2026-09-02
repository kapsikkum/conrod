#!/usr/bin/env python
"""Conrod (local) — command line entry point.

    python cli.py run   D:\\shoots\\bathurst-2026 --map entries.csv
    python cli.py review
    python cli.py write 3 --map entries.csv
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from conrod import store
from conrod.config import DEFAULTS, Settings
from conrod.mapping import NumberMap
from conrod.pipeline import run, write_job


def _progress_printer():
    """One rewriting status line per stage, so a long run stays readable."""
    last = {"stage": None, "at": 0.0}

    def report(event: dict) -> None:
        now = time.monotonic()
        stage = event.get("stage")
        # Throttle, but always show a stage change and always show completion.
        if (stage == last["stage"] and now - last["at"] < 0.25
                and event.get("done") != event.get("total")):
            return
        last["stage"], last["at"] = stage, now
        done, total = event.get("done", 0), event.get("total", 0)
        bar = f"{done}/{total}" if total else str(done)
        sys.stdout.write(f"\r\033[K[{stage}] {bar}  {event.get('message', '')}")
        sys.stdout.flush()
        if stage in {"warn"}:
            sys.stdout.write("\n")

    return report


def _settings_from(args: argparse.Namespace) -> Settings:
    settings = Settings()
    for field in ("detect_model", "detect_conf", "detect_imgsz", "vlm_model",
                  "vlm_host", "ocr_accept_confidence", "min_box_fraction",
                  "keyword_prefix"):
        value = getattr(args, field, None)
        if value is not None:
            setattr(settings, field, value)
    if getattr(args, "no_vlm", False):
        settings.use_vlm = False
    if getattr(args, "embed_in_raw", False):
        settings.write_sidecar_for_raw = False
    return settings


def _load_map(path: str | None) -> NumberMap:
    if not path:
        return NumberMap()
    mapping = NumberMap.load(Path(path))
    print(f"Loaded {len(mapping)} entries from {path}")
    return mapping


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    root = Path(args.folder)
    if not root.is_dir():
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2

    started = time.monotonic()
    summary = run(root, settings, label=args.label,
                  recursive=not args.no_recurse, on_progress=_progress_printer())
    elapsed = time.monotonic() - started

    print(f"\n\nJob {summary.job_id}: {summary.images} frames, "
          f"{summary.detections} vehicles, {summary.identified} identified "
          f"in {elapsed / 60:.1f} min")
    print(f"Review them with:  python cli.py review")
    print(f"Then write with:   python cli.py write {summary.job_id}"
          + (f" --map {args.map}" if args.map else ""))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    import uvicorn

    from conrod import server

    server.configure(_settings_from(args), _load_map(args.map))
    print(f"Review UI on http://{args.host}:{args.port}")
    uvicorn.run(server.app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    mapping = _load_map(args.map)

    job_id = args.job_id
    if job_id is None:
        with store.session() as conn:
            row = store.latest_job(conn)
        if not row:
            print("No jobs in the database yet.", file=sys.stderr)
            return 2
        job_id = row["id"]
        print(f"Using most recent job {job_id} ({row['label']})")

    result = write_job(job_id, settings, mapping, dry_run=args.dry_run,
                       on_progress=_progress_printer())
    print()
    if args.dry_run:
        print(f"\nDry run: {result['frames']} frames would be keyworded.")
    else:
        print(f"\nWrote {result['written']} of {result['frames']} frames "
              f"({result['failed']} failed).")
    return 0


def cmd_jobs(_args: argparse.Namespace) -> int:
    with store.session() as conn:
        rows = store.list_jobs(conn)
    if not rows:
        print("No jobs yet.")
        return 0
    print(f"{'id':>4}  {'status':<9} {'frames':>7} {'vehicles':>9}  label")
    for row in rows:
        print(f"{row['id']:>4}  {row['status']:<9} {row['image_count']:>7} "
              f"{row['detection_count']:>9}  {row['label']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conrod",
        description="Local race-number keywording for motorsport photography.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--map", help="CSV mapping race numbers to drivers/teams")
        p.add_argument("--vlm-model", default=None,
                       help=f"Ollama vision model (default {DEFAULTS.vlm_model})")
        p.add_argument("--vlm-host", default=None)
        p.add_argument("--keyword-prefix", default=None,
                       help="Prefix every written keyword, e.g. 'TA:'")

    run_p = sub.add_parser("run", help="scan a folder, detect vehicles, read numbers")
    run_p.add_argument("folder")
    run_p.add_argument("--label", help="name for this job")
    run_p.add_argument("--no-recurse", action="store_true")
    run_p.add_argument("--no-vlm", action="store_true",
                       help="OCR only; much faster, noticeably less accurate")
    run_p.add_argument("--detect-model", default=None,
                       help=f"YOLO weights (default {DEFAULTS.detect_model})")
    run_p.add_argument("--detect-conf", type=float, default=None)
    run_p.add_argument("--detect-imgsz", type=int, default=None)
    run_p.add_argument("--min-box-fraction", type=float, default=None,
                       help="ignore vehicles smaller than this fraction of the frame")
    run_p.add_argument("--ocr-accept-confidence", type=float, default=None,
                       help="OCR confidence above which the VLM is not consulted")
    add_common(run_p)
    run_p.set_defaults(func=cmd_run)

    review_p = sub.add_parser("review", help="open the local review UI")
    review_p.add_argument("--host", default="127.0.0.1")
    review_p.add_argument("--port", type=int, default=8760)
    add_common(review_p)
    review_p.set_defaults(func=cmd_review)

    write_p = sub.add_parser("write", help="write keywords into XMP")
    write_p.add_argument("job_id", nargs="?", type=int,
                         help="defaults to the most recent job")
    write_p.add_argument("--dry-run", action="store_true")
    write_p.add_argument("--embed-in-raw", action="store_true",
                         help="write into the RAW file instead of a .xmp sidecar")
    add_common(write_p)
    write_p.set_defaults(func=cmd_write)

    jobs_p = sub.add_parser("jobs", help="list jobs")
    jobs_p.set_defaults(func=cmd_jobs)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved; rerun to continue.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
