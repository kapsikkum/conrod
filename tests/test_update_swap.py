"""The folder swap that installs an update.

This is the one part of Conrod that runs after the app has quit, with no
window and nobody watching, so when it goes wrong it goes wrong in silence.
It has twice: once holding the folder it was moving (an inherited working
directory), once not running at all (DETACHED_PROCESS, which leaves
PowerShell with no console and makes it exit before reading the script).
Neither showed up as an error anywhere. Hence a test that actually starts
the script and watches the files move.
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from conrod.update import _SWAP_SCRIPT, launch_swap

WINDOWS = sys.platform == "win32"


def _script_for(root: Path, pid: int) -> tuple[Path, Path, Path]:
    """Lay out a fake install and write the swap script that updates it."""
    target = root / "app" / "Conrod"
    staging = root / "app" / "Conrod-update"
    new = staging / "Conrod"
    updates = root / "updates"
    for folder in (target, new, updates):
        folder.mkdir(parents=True)
    (target / "Conrod.exe").write_text("OLD")
    (new / "Conrod.exe").write_text("NEW")

    script = updates / "swap.ps1"
    script.write_text(
        _SWAP_SCRIPT
        .replace("@PID@", str(pid))
        .replace("@NEW@", str(new))
        .replace("@TARGET@", str(target))
        .replace("@OLD@", str(root / "app" / "Conrod-previous"))
        .replace("@STAGING@", str(staging))
        .replace("@LOG@", str(updates / "swap.log")),
        encoding="utf-8")
    return script, updates, target


def _wait_for(check, seconds: int = 60) -> bool:
    for _ in range(seconds * 2):
        if check():
            return True
        time.sleep(0.5)
    return False


@unittest.skipUnless(WINDOWS, "the swap script is PowerShell")
class Swap(unittest.TestCase):
    def test_replaces_the_folder_it_was_launched_from(self) -> None:
        """The whole job: old build out, new build in, nothing left behind.

        The stand-in for Conrod is started *inside* the folder being moved,
        because that is where Conrod runs from and where the swap script
        used to end up standing too.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app" / "Conrod").mkdir(parents=True)
            victim = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", "Start-Sleep 3"],
                cwd=str(root / "app" / "Conrod"))
            (root / "app" / "Conrod").rmdir()

            script, updates, target = _script_for(root, victim.pid)
            swap = launch_swap(script, updates)
            exe = target / "Conrod.exe"

            self.assertTrue(
                _wait_for(lambda: exe.is_file() and exe.read_text() == "NEW"),
                "the new build never arrived; swap.log says: " + (
                    (updates / "swap.log").read_text(encoding="utf-8-sig")
                    if (updates / "swap.log").exists() else "nothing at all"))
            self.assertFalse((root / "app" / "Conrod-update").exists())
            self.assertFalse((root / "app" / "Conrod-previous").exists())
            victim.wait(timeout=30)
            # It holds its own working directory until it exits, and the
            # temporary folder cannot be removed while it does.
            swap.wait(timeout=60)

    def test_says_what_it_did(self) -> None:
        """A silent failure is the failure mode. It has to leave a log."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, updates, target = _script_for(root, 0x7FFFFFFF)
            swap = launch_swap(script, updates)
            log = updates / "swap.log"
            self.assertTrue(_wait_for(log.exists),
                            "the swap script ran without recording anything")
            _wait_for(lambda: "build" in log.read_text(encoding="utf-8-sig"))
            self.assertIn("swap starting", log.read_text(encoding="utf-8-sig"))
            swap.wait(timeout=60)


@unittest.skipUnless(WINDOWS, "creation flags are a Windows concern")
class Launching(unittest.TestCase):
    def test_the_script_actually_runs(self) -> None:
        """DETACHED_PROCESS passed this by starting a process that did nothing.

        It exited 0, so nothing looked wrong. Assert on the side effect.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            proof = root / "ran.txt"
            script = root / "swap.ps1"
            script.write_text(
                f"'ran' | Out-File -FilePath '{proof}' -Encoding utf8\n",
                encoding="utf-8")
            launch_swap(script, root).wait(timeout=60)
            self.assertTrue(_wait_for(proof.exists, 30),
                            "the launcher started a process that ran nothing")


if __name__ == "__main__":
    unittest.main()
