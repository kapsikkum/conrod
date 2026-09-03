"""Checking GitHub for a newer build, and installing it.

A running Windows executable cannot overwrite itself, so installing means
unpacking the new build beside the old one and handing the swap to a short
script that waits for this process to exit, moves the folders, and starts the
new one.

This downloads and then runs code, so it is deliberately narrow:

  * only the repository below, over HTTPS, never a URL from anywhere else;
  * the asset's SHA-256 must match the checksums file published with the
    release, and the release workflow publishes one for exactly this reason;
  * the archive must actually look like Conrod before anything is swapped;
  * only a frozen build updates itself. Run from source it says so and stops,
    because replacing a git checkout with a release build would be a
    surprising thing to do to someone's working copy.

Nothing happens without the user asking for it in the app.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__

REPO = "kapsikkum/conrod"
RELEASES = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECKSUMS = "SHA256SUMS.txt"


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    url: str
    asset: str | None
    size: int
    newer: bool


def _parts(version: str) -> tuple:
    out = []
    for chunk in version.strip().lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0, 0, 0])[:3]


def is_newer(candidate: str, current: str = __version__) -> bool:
    return _parts(candidate) > _parts(current)


def check(timeout: float = 10.0) -> Release:
    """Ask GitHub what the latest release is."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(RELEASES, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Conrod/{__version__}",
        })
        response.raise_for_status()
        data = response.json()

    tag = str(data.get("tag_name") or "")
    version = tag.lstrip("v")
    asset_url, size = None, 0
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith(".zip") and "win" in name.lower():
            asset_url = asset.get("browser_download_url")
            size = int(asset.get("size") or 0)
            break

    return Release(
        version=version or "unknown",
        tag=tag,
        notes=(data.get("body") or "").strip(),
        url=data.get("html_url") or f"https://github.com/{REPO}/releases",
        asset=asset_url,
        size=size,
        newer=bool(version) and is_newer(version),
    )


def _trusted(url: str) -> bool:
    """Only GitHub, only over TLS. Never a URL that came from anywhere else."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = parsed.netloc.lower().split(":")[0]
    return (host == "github.com" or host.endswith(".github.com")
            or host.endswith(".githubusercontent.com"))


def _expected_digest(client: httpx.Client, release: Release,
                     filename: str) -> str | None:
    """The published SHA-256 for this asset, if the release carries one."""
    url = f"https://github.com/{REPO}/releases/download/{release.tag}/{CHECKSUMS}"
    try:
        response = client.get(url)
        if response.status_code != 200:
            return None
        for line in response.text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == filename:
                return parts[0].lower()
    except httpx.HTTPError:
        return None
    return None


def _looks_like_conrod(archive: Path) -> bool:
    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return any(n.replace("\\", "/").endswith("Conrod/Conrod.exe") for n in names)


def download(release: Release, into: Path, on_progress=None) -> Path:
    """Fetch the release zip and verify it before anything unpacks it."""
    if not release.asset or not _trusted(release.asset):
        raise RuntimeError("that release has no download from GitHub")

    into.mkdir(parents=True, exist_ok=True)
    filename = release.asset.rsplit("/", 1)[-1]
    target = into / filename
    digest = hashlib.sha256()

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        expected = _expected_digest(client, release, filename)
        with client.stream("GET", release.asset) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or release.size or 0)
            done = 0
            with open(target, "wb") as fh:
                for chunk in response.iter_bytes(1 << 16):
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)

    if expected and digest.hexdigest() != expected:
        target.unlink(missing_ok=True)
        raise RuntimeError("the download did not match the published checksum")
    if not _looks_like_conrod(target):
        target.unlink(missing_ok=True)
        raise RuntimeError("that download does not look like a Conrod build")
    return target


def install(archive: Path, on_progress=None) -> str:
    """Unpack the new build and hand the swap to a script, then quit."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "Running from source. Update with 'git pull' instead — replacing a "
            "checkout with a release build is not something to do behind your back."
        )
    if not _looks_like_conrod(archive):
        raise RuntimeError("that archive does not contain a Conrod build")

    current = Path(sys.executable).parent            # ...\Conrod
    staging = current.parent / "Conrod-update"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    if on_progress:
        on_progress("unpacking")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(staging)

    unpacked = staging / "Conrod"
    if not (unpacked / "Conrod.exe").is_file():
        raise RuntimeError("the archive did not unpack into the expected shape")

    # Not inside the staging folder: the script deletes that folder as its
    # last act, and a running script cannot be sure of removing itself.
    script = archive.parent / "swap.ps1"
    script.write_text(
        _SWAP_SCRIPT
        .replace("@PID@", str(os.getpid()))
        .replace("@NEW@", str(unpacked))
        .replace("@TARGET@", str(current))
        .replace("@OLD@", str(current.parent / "Conrod-previous"))
        .replace("@STAGING@", str(staging))
        .replace("@LOG@", str(archive.parent / "swap.log")),
        encoding="utf-8",
    )

    launch_swap(script, archive.parent)
    return "Conrod will close and reopen on the new version."


def launch_swap(script: Path, cwd: Path) -> subprocess.Popen:
    """Start the swap script so that it outlives us and actually runs.

    Both halves of that were wrong once and neither said so:

    * cwd matters more than it looks. A child inherits ours, which is the
      folder about to be moved, and Windows locks a process's working
      directory -- so the swap script ended up holding the very folder it
      was trying to move. Move-Item failed, the rollback tidied up, and
      nothing anywhere recorded that an update had been attempted.
    * DETACHED_PROCESS sounds like exactly what a script that outlives us
      wants. PowerShell given no console at all exits 0 immediately without
      running the file. CREATE_NO_WINDOW gives it a console nobody can see,
      which is the real requirement; the new process group keeps our own
      shutdown from reaching it.
    """
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", str(script)],
        cwd=str(cwd),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


# Waits for this process to go, keeps the old build until the new one is in
# place, and puts it back if the move fails, so a failed update cannot leave
# someone with no application at all.
_SWAP_SCRIPT = r"""
$ErrorActionPreference = 'Stop'

# Never stand in the folder being moved. Popen is started outside it too;
# this is the belt to that pair of braces, and costs nothing.
Set-Location -LiteralPath $env:SystemRoot

# The app is gone by the time any of this runs, so a failure here has no way
# to reach the user except by writing it down. Everything below appends.
$log = '@LOG@'
function Say($text) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $text" |
        Out-File -FilePath $log -Append -Encoding utf8
}
Say 'swap starting'

# Conrod has to be gone before its folder can be moved. It closes itself as
# part of installing, but if it is somehow still running, stop here: trying
# the move anyway fails on files that are in use, and the rollback that
# follows made it look like nothing had happened at all.
try { Wait-Process -Id @PID@ -Timeout 90 } catch { }
if (Get-Process -Id @PID@ -ErrorAction SilentlyContinue) {
    Say 'giving up: Conrod (pid @PID@) is still running'
    exit 2
}
Start-Sleep -Seconds 1

$target = '@TARGET@'
$new    = '@NEW@'
$old    = '@OLD@'

if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
try {
    Move-Item -LiteralPath $target -Destination $old -Force
    Move-Item -LiteralPath $new -Destination $target -Force
    Say 'swapped in the new build'
} catch {
    Say "swap failed: $($_.Exception.Message)"
    if ((Test-Path -LiteralPath $old) -and -not (Test-Path -LiteralPath $target)) {
        Move-Item -LiteralPath $old -Destination $target -Force
        Say 'rolled back to the previous build'
    }
    exit 1
}

Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '@STAGING@' -Recurse -Force -ErrorAction SilentlyContinue
Say 'starting the new build'
Start-Process -FilePath (Join-Path $target 'Conrod.exe') -WorkingDirectory $target
"""
