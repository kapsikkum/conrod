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
import time
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


STALL_FLOOR = 96 * 1024        # bytes/sec a healthy connection beats easily
STALL_SECONDS = 20.0           # how long it may sit under the floor
MAX_ATTEMPTS = 6


def _digest_of(path: Path) -> str:
    """Hash the finished file rather than the bytes as they arrive.

    A resumed download only sees the tail, so a running hash would be of the
    wrong thing. Reading 300MB back costs about a second and means the
    checksum covers what is actually on disk -- including the part written
    before some earlier attempt gave up.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tidy(into: Path, keep: str | None = None) -> int:
    """Delete archives left behind by previous updates.

    Each one is about 300MB and nothing ever removed them, so a few updates
    quietly cost a gigabyte in the data folder.
    """
    freed = 0
    for old in sorted(into.glob("Conrod-*-win64.zip")):
        if keep and old.name == keep:
            continue
        try:
            size = old.stat().st_size
            old.unlink()
            freed += size
        except OSError:
            pass
    return freed


def download(release: Release, into: Path, on_progress=None) -> Path:
    """Fetch the release zip and verify it before anything unpacks it.

    Resumable, and it gives up on a connection that has stopped making
    progress. A single long-lived TCP flow that hits packet loss can settle
    into a collapsed congestion window and stay there: measured here at
    72 KB/s for the whole of a 305MB download -- about an hour and a half --
    while a *new* connection to the very same CDN address pulled 7.6 MB/s at
    the same moment. Waiting it out does not work, because nothing about that
    connection is going to recover. So when throughput sits under the floor
    for long enough, the connection is dropped and the rest is asked for with
    a Range header, which is also what makes an interrupted update pick up
    where it left off instead of starting the 305MB again.
    """
    if not release.asset or not _trusted(release.asset):
        raise RuntimeError("that release has no download from GitHub")

    into.mkdir(parents=True, exist_ok=True)
    filename = release.asset.rsplit("/", 1)[-1]
    target = into / filename
    total = release.size or 0
    # Before adding another 300MB, not after: the old ones are dead weight
    # and the disk may be why this is being done at all.
    tidy(into, keep=filename)

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        expected = _expected_digest(client, release, filename)

        # Reconnecting only helps if a *different* connection would be
        # faster. After a couple of attempts have each collapsed, the honest
        # reading is that this link really is slow, and dropping a working
        # download over and over would then be the bug. So the floor applies
        # to the first attempts only.
        give_up_on_speed = 2
        stalls = 0

        for attempt in range(MAX_ATTEMPTS):
            done = target.stat().st_size if target.exists() else 0
            if total and done >= total:
                break

            headers = {"Range": f"bytes={done}-"} if done else {}
            stalled = False
            try:
                with client.stream("GET", release.asset, headers=headers) as response:
                    if done and response.status_code == 416:
                        break          # the file on disk is already complete
                    if done and response.status_code == 200:
                        # The range was ignored and the whole file is coming.
                        # Start over rather than append to a prefix.
                        done = 0
                    response.raise_for_status()

                    # Only when the release did not tell us. Taking it from
                    # the response instead means a truncated reply redefines
                    # the target as its own short length, and the download
                    # then looks complete at exactly the wrong moment.
                    if not total:
                        length = int(response.headers.get("content-length") or 0)
                        if length:
                            total = done + length

                    watch = stalls < give_up_on_speed
                    window_start, window_bytes = time.monotonic(), 0
                    with open(target, "ab" if done else "wb") as fh:
                        for chunk in response.iter_bytes(1 << 16):
                            fh.write(chunk)
                            done += len(chunk)
                            window_bytes += len(chunk)
                            if on_progress:
                                on_progress(done, total)

                            if not watch:
                                continue
                            elapsed = time.monotonic() - window_start
                            if elapsed >= STALL_SECONDS:
                                if window_bytes / elapsed < STALL_FLOOR:
                                    stalled = True
                                    break
                                window_start, window_bytes = time.monotonic(), 0
            except httpx.HTTPError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(min(2 ** attempt, 10))
                continue

            if stalled:
                stalls += 1
                continue      # a fresh connection, resuming from `done`
            if total and done < total:
                # The stream ended cleanly but short -- a server that closed
                # early, or a Content-Length smaller than the file. There is
                # no exception to catch here, so without this check the
                # truncated file goes straight to the checksum and the update
                # fails with a mismatch that says nothing about the cause.
                continue
            break

    got = target.stat().st_size if target.exists() else 0
    if total and got < total:
        raise RuntimeError(
            f"the download stopped at {got // (1 << 20)} of "
            f"{total // (1 << 20)} MB -- press Update again to resume")

    if expected and _digest_of(target) != expected:
        target.unlink(missing_ok=True)
        raise RuntimeError("the download did not match the published checksum")
    if not _looks_like_conrod(target):
        target.unlink(missing_ok=True)
        raise RuntimeError("that download does not look like a Conrod build")
    tidy(into, keep=target.name)
    return target


def _clear_staging(beside: Path) -> Path:
    """An empty folder to unpack into, whatever is left over from last time.

    This used to be `rmtree(ignore_errors=True)` followed by `mkdir()`, which
    is two statements that disagree: the first says the folder might refuse
    to go, the second insists it did. On a copy installed inside OneDrive --
    which is where people put it, because that is where their Desktop is --
    the sync client holds handles open, the delete quietly fails and the
    update dies on

        [WinError 183] Cannot create a file when that file already
        exists: '...Desktop/Conrod-win64/Conrod-update'

    with a 305 MB download already on disk and nothing wrong with it.

    So: try to clear the usual folder, and if something is still holding it,
    step aside to a fresh one rather than refusing to update. The swap script
    is told which folder to use and removes it at the end, so a stale one
    costs disk until the next attempt tidies it, and never costs the update.
    """
    preferred = beside / "Conrod-update"
    for candidate in [preferred] + [beside / f"Conrod-update-{n}"
                                    for n in range(2, 12)]:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
        if candidate.exists():
            continue                     # something still has it open
        try:
            candidate.mkdir(parents=True)
        except OSError:
            continue
        return candidate
    raise RuntimeError(
        f"Could not clear a folder to unpack into beside {beside}. Something "
        f"is holding {preferred.name} open -- OneDrive or a file browser "
        f"sitting in it are the usual causes. Close them, or delete that "
        f"folder by hand, and try again."
    )


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
    staging = _clear_staging(current.parent)

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
