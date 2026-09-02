"""ExifTool plumbing.

Spawning exiftool per file costs ~300ms on Windows because it is Perl, which
would dominate the run on a 3000-frame shoot. Two ways around that are used
here: a persistent ``-stay_open`` process for tag reads and writes, and
exiftool's own batch ``-w`` mode for bulk preview extraction, which keeps the
binary data off the pipe entirely.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image

from .config import find_exiftool

# A windowed build has no console, so each exiftool spawn would otherwise
# flash a black window. Zero on any platform that lacks the flag.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ExifTool:
    """A persistent exiftool process.

    Used as a context manager. Not safe to share across threads without the
    lock this class already holds internally.
    """

    SENTINEL = "{ready}"

    def __init__(self, executable: str | None = None):
        self.executable = executable or find_exiftool()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def __enter__(self) -> "ExifTool":
        self._proc = subprocess.Popen(
            [self.executable, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=NO_WINDOW,
        )
        return self

    def __exit__(self, *exc) -> None:
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.write("-stay_open\nFalse\n")
                proc.stdin.flush()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    def execute(self, *args: str) -> str:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError("ExifTool is not running; use it as a context manager.")
        with self._lock:
            self._proc.stdin.write("\n".join(args) + f"\n-execute\n")
            self._proc.stdin.flush()
            chunks: list[str] = []
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("exiftool exited unexpectedly")
                if line.strip().startswith(self.SENTINEL):
                    break
                chunks.append(line)
        return "".join(chunks)

    def read_tags(self, paths: Sequence[Path], tags: Sequence[str]) -> list[dict]:
        """Read the given tags for a batch of files."""
        if not paths:
            return []
        args = ["-json", "-charset", "filename=utf8"]
        args += [f"-{t}" for t in tags]
        args += [str(p) for p in paths]
        out = self.execute(*args).strip()
        if not out:
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return []


def extract_previews(
    files: Sequence[Path],
    out_dir: Path,
    executable: str | None = None,
) -> dict[Path, Path]:
    """Pull the embedded JPEG preview out of each RAW file.

    Returns a mapping of source path -> extracted preview path. Files whose
    preview could not be extracted are simply absent from the mapping.

    Detection never needs the full RAW: the embedded preview is a
    camera-rendered JPEG, typically near full resolution, and reading it
    avoids a demosaic that would cost seconds per frame.
    """
    exe = executable or find_exiftool()
    result: dict[Path, Path] = {}
    if not files:
        return result

    # Group by source directory and mirror that structure under the cache, so
    # two shoots with an IMG_0001.CR3 each cannot collide on one cache name.
    by_dir: dict[Path, list[Path]] = {}
    for f in files:
        by_dir.setdefault(f.parent, []).append(f)

    for src_dir, group in by_dir.items():
        dest = out_dir / _mirror_name(src_dir)
        dest.mkdir(parents=True, exist_ok=True)

        # Largest first. On a Canon CR3, JpgFromRaw is the full-resolution
        # camera JPEG while PreviewImage is only about 1620x1080 — far too
        # small to read a plate or a sponsor decal from.
        for tag in ("JpgFromRaw", "PreviewImage", "OtherImage", "ThumbnailImage"):
            pending = [f for f in group if not (dest / f"{f.stem}.jpg").exists()]
            if not pending:
                break
            _run_batch_extract(exe, tag, pending, dest)

        extracted = {}
        for f in group:
            candidate = dest / f"{f.stem}.jpg"
            if candidate.exists() and candidate.stat().st_size > 0:
                extracted[f] = candidate

        _apply_orientation(exe, extracted)
        result.update(extracted)

    return result


def _apply_orientation(exe: str, extracted: dict[Path, Path]) -> None:
    """Rotate previews to match the orientation recorded in the RAW.

    An embedded preview is stored in sensor order and carries no orientation
    tag of its own, so a portrait frame arrives on its side and every detector
    downstream sees a sideways car. The tag lives on the RAW, not the JPEG, so
    it has to be read separately and baked into the cached preview.
    """
    if not extracted:
        return
    marker = ".oriented"
    todo = [src for src, jpg in extracted.items()
            if not jpg.with_suffix(jpg.suffix + marker).exists()]
    if not todo:
        return

    with ExifTool(exe) as tool:
        # -n gives the numeric EXIF value rather than "Rotate 270 CW".
        rows = tool.read_tags(todo, ["Orientation#"])

    by_source = {Path(r.get("SourceFile", "")).resolve(): r for r in rows}
    for src in todo:
        jpg = extracted[src]
        row = by_source.get(src.resolve(), {})
        try:
            orientation = int(row.get("Orientation") or 1)
        except (TypeError, ValueError):
            orientation = 1

        transpose = {3: Image.ROTATE_180, 6: Image.ROTATE_270, 8: Image.ROTATE_90}
        if orientation in transpose:
            try:
                with Image.open(jpg) as img:
                    img.transpose(transpose[orientation]).save(jpg, "JPEG", quality=94)
            except Exception:
                pass
        # Mark it done either way, so a re-run does not rotate twice.
        jpg.with_suffix(jpg.suffix + marker).write_text("", encoding="utf-8")


def _run_batch_extract(exe: str, tag: str, files: Sequence[Path], dest: Path) -> None:
    """One exiftool invocation that writes <stem>.jpg for every input file."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".args", delete=False, encoding="utf-8"
    ) as fh:
        arg_path = Path(fh.name)
        fh.write("-b\n")
        fh.write(f"-{tag}\n")
        fh.write("-w\n")
        # No '!' — do not clobber a preview an earlier, higher-priority tag
        # already produced.
        fh.write(f"{dest}/%f.jpg\n")
        fh.write("-charset\n")
        fh.write("filename=utf8\n")
        for f in files:
            fh.write(f"{f}\n")
    try:
        subprocess.run(
            [exe, "-@", str(arg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=NO_WINDOW,
        )
    finally:
        arg_path.unlink(missing_ok=True)


def _mirror_name(directory: Path) -> str:
    """A filesystem-safe cache folder name derived from a source directory."""
    raw = str(directory.resolve())
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)
    # Keep it short but collision-resistant.
    import hashlib

    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{safe[-60:]}_{digest}"
