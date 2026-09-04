"""The browser code has to at least be valid JavaScript.

Cheap, and it earns its place: a syntax error in app.js does not fail
loudly. The page loads, the stylesheet applies, every button is drawn, and
nothing works -- no album list, no stacks, no handlers -- because the whole
file failed to evaluate. It took a console read to find, and the symptom
looked like a data problem rather than a broken file.

Skipped where node is not installed, so this never blocks a local run on a
machine without it. CI has node.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

import conrod

WEB = Path(conrod.__file__).parent / "web"


class TheBrowserCode(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")

    def test_app_js_parses(self) -> None:
        result = subprocess.run(
            [self.node, "--check", str(WEB / "app.js")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TheMarkupIsWiredToTheCode(unittest.TestCase):
    """Every id the code reaches for exists in the page.

    A rename that misses one leaves $("#btn-thing") returning null and the
    line after it throwing, which takes the rest of the file with it.
    """

    def test_every_element_the_code_reaches_for_is_in_the_page(self) -> None:
        """Every $("#id"), not only the ones an onclick is hung on.

        This started life checking onclick bindings and would not have caught
        the bug it was written for. A "Not the main car" button was removed
        from the page when Reject was rewired to do that job, and the line
        that set its .hidden stayed behind. It threw on every attempt to open
        a frame, which took the whole full-frame view down with it -- clicking
        a picture to see it larger stopped working, in a shipped release, and
        nothing anywhere said why.
        """
        import re

        code = (WEB / "app.js").read_text(encoding="utf-8")
        html = (WEB / "index.html").read_text(encoding="utf-8")
        wanted = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', code))
        missing = sorted(i for i in wanted if f'id="{i}"' not in html)
        self.assertEqual(
            missing, [],
            f"app.js reaches for these, and the page has none of them: {missing}")


if __name__ == "__main__":
    unittest.main()
