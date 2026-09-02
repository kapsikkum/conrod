"""First-run environment checks, and the actions that fix them.

The wizard asks this module what is missing and offers to put it right. It is
deliberately honest about what cannot be bundled: the vision model is ~6 GB and
runs under Ollama, so a fresh machine needs a download no executable can carry.
"""

from __future__ import annotations

import os
import shutil
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


def inspect(settings: Settings) -> Environment:
    """Everything the app needs, and whether it is here."""
    env = Environment()

    # --- ExifTool: required, cannot work without it ---
    try:
        from .config import find_exiftool

        exe = find_exiftool()
        version = subprocess.run([exe, "-ver"], capture_output=True, text=True,
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

    # --- plate detector: downloaded by its own package on first use ---
    env.checks.append(Check(
        "plates", "Plate detector", True,
        f"{settings.plate_model} — 7.5 MB, downloads on first use",
        required=False))

    # --- Ollama and the vision model: optional but this is the good part ---
    if not settings.use_vlm:
        env.checks.append(Check("vlm", "Vision model", True,
                                "disabled in settings", required=False))
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
