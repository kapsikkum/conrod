"""First-run environment checks, and the actions that fix them.

The wizard asks this module what is missing and offers to put it right. It is
deliberately honest about what cannot be bundled: the vision model is ~6 GB and
runs under Ollama, so a fresh machine needs a download no executable can carry.
"""

from __future__ import annotations

import os
import shutil
import importlib.util
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import MODEL_DIR, Settings

OLLAMA_DOWNLOAD = "https://ollama.com/download"
EXIFTOOL_DOWNLOAD = "https://exiftool.org/"


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str = ""
    required: bool = True
    fix: str | None = None          # 'pull_model' | 'download_weights' | None
    link: str | None = None


@dataclass
class Environment:
    checks: list[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.required and not c.ok]

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "checks": [vars(c) for c in self.checks],
        }


def _reader_check(key: str, label: str, wanted: bool,
                  modules, detail: str) -> Check:
    """Whether a reader can actually run, rather than whether it is switched on.

    ``modules`` is (import name, what it does) pairs. Checked by importlib
    rather than by importing them: this runs on the way to the first screen
    and some of these pull onnxruntime with them, which is not a cost worth
    paying to answer a question about whether a file exists.

    Never required. A shoot still scans, rates and identifies without any of
    this -- it just will not read plates or numbers, which is a thing to say
    plainly rather than to leave someone to infer from an empty column.
    """
    if not wanted:
        return Check(key, label, True, "turned off in settings", required=False)

    missing = [(name, does) for name, does in modules
               if importlib.util.find_spec(name) is None]
    if not missing:
        return Check(key, label, True, detail, required=False)
    return Check(
        key, label, False,
        "not installed: " + ", ".join(f"{name} ({does})" for name, does in missing)
        + ". Nothing will be read off a plate until it is there —"
        " pip install -r requirements.txt",
        required=False)


def inspect(settings: Settings) -> Environment:
    """Everything the app needs, and whether it is here."""
    env = Environment()

    # --- ExifTool: required, cannot work without it ---
    try:
        from .config import find_exiftool

        exe = find_exiftool()
        version = subprocess.run([exe, "-ver"], capture_output=True, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                 timeout=15).stdout.strip()
        env.checks.append(Check("exiftool", "ExifTool", True,
                                f"version {version}"))
    except Exception as exc:
        env.checks.append(Check(
            "exiftool", "ExifTool", False, str(exc),
            required=True, link=EXIFTOOL_DOWNLOAD))

    # --- vehicle detector weights: downloadable ---
    weights = MODEL_DIR / settings.detect_model
    env.checks.append(Check(
        "detector", "Vehicle detector", weights.exists(),
        f"{settings.detect_model}"
        + (f" ({weights.stat().st_size // 1_000_000} MB)" if weights.exists()
           else " — will download on first run, about 19 MB"),
        required=False, fix=None if weights.exists() else "download_weights"))

    # --- plate and text reading: the packages are what decide this ---
    #
    # This used to report True unconditionally, which made it worse than no
    # check at all. The imports behind plates and OCR are deliberately lazy,
    # so a missing one does not break the build or raise anywhere a person
    # would see it -- reading simply stops happening, and the app goes on
    # saying "Plate detector: downloads on first use" while returning nothing
    # on every frame of the shoot. That is how 0.1.0 shipped without plate
    # reading, and requirements.txt has a note about it.
    env.checks.append(_reader_check(
        "plates", "Plate reading", settings.read_plates,
        (("open_image_models", "finding the plate"),
         ("fast_plate_ocr", "reading the characters")),
        f"{settings.plate_model} — 7.5 MB, downloads on first use"))
    env.checks.append(_reader_check(
        "ocr", "Number and text reading", settings.read_numbers or settings.read_text,
        (("rapidocr_onnxruntime", "reading numbers and livery text"),),
        "PaddleOCR models on onnxruntime"))

    # --- The vision model: optional, but this is the good part ---
    if not settings.use_vlm:
        env.checks.append(Check("vlm", "Vision model", True,
                                "disabled in settings", required=False))
        return env

    # Only Ollama has anything to install. Offering to pull "claude-sonnet-5"
    # as a 6 GB download -- which is what this did once a provider could be
    # something other than Ollama -- is nonsense: there is nothing local to
    # fetch, the question is whether the key works.
    provider = (getattr(settings, "vlm_provider", "ollama") or "ollama").lower()
    if provider != "ollama":
        from .vlm_providers import DISPLAY_NAMES

        name = DISPLAY_NAMES.get(provider, provider)
        if not getattr(settings, "vlm_api_key", ""):
            env.checks.append(Check(
                "vlm", "Vision model", False,
                f"{settings.vlm_model} via {name} — no API key set. "
                "Add one in Settings.",
                required=False))
        else:
            env.checks.append(Check(
                "vlm", "Vision model", True,
                f"{settings.vlm_model} via {name}", required=False))
        return env

    reachable, models = _ollama_status(settings)
    if not reachable:
        env.checks.append(Check(
            "ollama", "Ollama", False,
            f"not reachable at {settings.vlm_host}. Without it the app still "
            "reads plates, numbers and text, but cannot identify make, model, "
            "colour or team.",
            required=False, link=OLLAMA_DOWNLOAD))
        return env

    env.checks.append(Check("ollama", "Ollama", True, "running"))
    has_model = settings.vlm_model in models or f"{settings.vlm_model}:latest" in models
    env.checks.append(Check(
        "vlm", "Vision model", has_model,
        f"{settings.vlm_model}" if has_model
        else f"{settings.vlm_model} not installed — about 6 GB to download",
        required=False, fix=None if has_model else "pull_model"))
    return env


def _ollama_status(settings: Settings) -> tuple[bool, set[str]]:
    try:
        resp = httpx.get(f"{settings.vlm_host}/api/tags", timeout=4.0)
        resp.raise_for_status()
        return True, {m.get("name", "") for m in resp.json().get("models", [])}
    except Exception:
        return False, set()


def ollama_binary() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
    return str(candidate) if candidate.exists() else None


def pull_model(settings: Settings, on_progress=None) -> bool:
    """Pull the vision model, reporting progress lines as they arrive."""
    try:
        with httpx.stream("POST", f"{settings.vlm_host}/api/pull",
                          json={"model": settings.vlm_model},
                          timeout=None) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not on_progress:
                    continue
                import json

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = event.get("status", "")
                total, done = event.get("total"), event.get("completed")
                if total and done:
                    on_progress({"status": status,
                                 "percent": round(done / total * 100, 1)})
                else:
                    on_progress({"status": status})
        return True
    except Exception as exc:
        if on_progress:
            on_progress({"status": f"failed: {exc}", "error": True})
        return False


def download_weights(settings: Settings, on_progress=None) -> bool:
    """Fetch the YOLO weights into the data directory."""
    try:
        from .detect import load_model

        if on_progress:
            on_progress({"status": f"downloading {settings.detect_model}"})
        load_model(settings)
        if on_progress:
            on_progress({"status": "done", "percent": 100})
        return True
    except Exception as exc:
        if on_progress:
            on_progress({"status": f"failed: {exc}", "error": True})
        return False


_fix_lock = threading.Lock()


def apply_fix(name: str, settings: Settings, on_progress=None) -> bool:
    """Run one repair action. Serialised — these are all large downloads."""
    if not _fix_lock.acquire(blocking=False):
        raise RuntimeError("another setup step is already running")
    try:
        if name == "pull_model":
            return pull_model(settings, on_progress)
        if name == "download_weights":
            return download_weights(settings, on_progress)
        raise ValueError(f"unknown fix: {name}")
    finally:
        _fix_lock.release()
