"""Downloading 300MB over a connection that has quietly collapsed.

A single long-lived TCP flow that hits packet loss can settle into a
congestion window it never recovers from. Measured on a real update: the app
sat at exactly 72 KB/s for the whole download -- an hour and a half for
305MB -- while a new connection to the same CDN address pulled 7.6 MB/s at
that same moment. Nothing was wrong with the network, the disk, the code or
the machine. The connection was simply dead and the updater had no way to
notice or escape.

These tests run against a real HTTP server on localhost, because the whole
question is what the client does with ranges, truncated responses and a
stream that stops paying its way. A mocked transport would only replay my
assumptions back at me.
"""

from __future__ import annotations

import http.server
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from conrod import update

PAYLOAD = bytes(range(256)) * 400          # 102,400 bytes


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD, honouring Range, with faults the test can ask for."""

    behaviour = {"mode": "normal", "seen": []}

    def log_message(self, *args) -> None:      # keep the test output clean
        pass

    def do_GET(self):                          # noqa: N802
        mode = self.behaviour["mode"]
        if self.path.endswith("SHA256SUMS.txt"):
            self.send_response(404)
            self.end_headers()
            return

        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
        self.behaviour["seen"].append(start)

        if start >= len(PAYLOAD):
            self.send_response(416)
            self.end_headers()
            return

        body = PAYLOAD[start:]

        if mode == "ignores_range" and start:
            # A server that answers 200 with the whole file anyway.
            body, start = PAYLOAD, 0

        if mode == "slow_once":
            # Dribble the file out under the floor, then behave next time --
            # the collapsed-connection case, in miniature.
            self.behaviour["mode"] = "normal"
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            if start:
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            self.end_headers()
            try:
                for i in range(0, len(body), 1024):
                    self.wfile.write(body[i:i + 1024])
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        if mode == "truncate_once":
            # Drop the connection halfway through the first attempt only.
            self.behaviour["mode"] = "normal"
            body = body[: len(body) // 2]

        code = 206 if start and mode != "ignores_range" else 200
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if code == 206:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Downloading(unittest.TestCase):
    def setUp(self):
        _Handler.behaviour = {"mode": "normal", "seen": []}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/Conrod-9.9.9-win64.zip"

        # _trusted() only allows github.com, and rightly so.
        real = update._trusted
        update._trusted = lambda url: True
        self.addCleanup(setattr, update, "_trusted", real)

        self.release = update.Release(
            version="9.9.9", tag="v9.9.9", notes="", url="",
            asset=self.url, size=len(PAYLOAD), newer=True)

        # The archive check would reject our payload; it is tested elsewhere.
        looks = update._looks_like_conrod
        update._looks_like_conrod = lambda p: True
        self.addCleanup(setattr, update, "_looks_like_conrod", looks)

    def test_a_plain_download_arrives_intact(self) -> None:
        with TemporaryDirectory() as tmp:
            got = update.download(self.release, Path(tmp))
            self.assertEqual(got.read_bytes(), PAYLOAD)

    def test_a_half_written_file_resumes_instead_of_restarting(self) -> None:
        """The point of the exercise: 150MB already on disk is not re-fetched."""
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "Conrod-9.9.9-win64.zip"
            target.write_bytes(PAYLOAD[:40000])

            got = update.download(self.release, Path(tmp))

            self.assertEqual(got.read_bytes(), PAYLOAD)
            self.assertEqual(_Handler.behaviour["seen"], [40000],
                             "should have asked for the tail, once")

    def test_a_dropped_connection_is_picked_up_where_it_stopped(self) -> None:
        _Handler.behaviour["mode"] = "truncate_once"
        with TemporaryDirectory() as tmp:
            got = update.download(self.release, Path(tmp))
            self.assertEqual(got.read_bytes(), PAYLOAD)
            self.assertEqual(len(_Handler.behaviour["seen"]), 2)
            self.assertGreater(_Handler.behaviour["seen"][1], 0,
                               "the second attempt must resume, not restart")

    def test_a_server_that_ignores_the_range_does_not_corrupt_the_file(self) -> None:
        """Appending a whole file onto a prefix would give a longer, wrong one.

        This is the failure that a checksum would catch and a user would
        experience as 'the update is broken', so it is worth not causing.
        """
        _Handler.behaviour["mode"] = "ignores_range"
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "Conrod-9.9.9-win64.zip"
            target.write_bytes(PAYLOAD[:40000])

            got = update.download(self.release, Path(tmp))
            self.assertEqual(got.read_bytes(), PAYLOAD)

    def test_a_complete_file_is_not_downloaded_again(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "Conrod-9.9.9-win64.zip"
            target.write_bytes(PAYLOAD)

            got = update.download(self.release, Path(tmp))
            self.assertEqual(got.read_bytes(), PAYLOAD)
            self.assertEqual(_Handler.behaviour["seen"], [],
                             "nothing should have been requested")

    def _impatient(self, floor: int, seconds: float, give_up: int = 2):
        """Shrink the stall thresholds so the test does not take 40 seconds."""
        for name, value in (("STALL_FLOOR", floor),
                            ("STALL_SECONDS", seconds)):
            self.addCleanup(setattr, update, name, getattr(update, name))
            setattr(update, name, value)

    def test_a_collapsed_connection_is_dropped_rather_than_endured(self) -> None:
        """The actual bug: 72 KB/s for 305MB, when reconnecting fixes it."""
        self._impatient(floor=200_000, seconds=0.3)
        _Handler.behaviour["mode"] = "slow_once"

        with TemporaryDirectory() as tmp:
            got = update.download(self.release, Path(tmp))
            self.assertEqual(got.read_bytes(), PAYLOAD)
            self.assertGreaterEqual(len(_Handler.behaviour["seen"]), 2,
                                    "should have given up on the slow one")
            self.assertGreater(_Handler.behaviour["seen"][1], 0,
                               "and resumed rather than started again")

    def test_a_genuinely_slow_link_is_allowed_to_finish(self) -> None:
        """Reconnecting only helps if another connection would be faster.

        Someone on a slow line would otherwise have their download dropped
        every twenty seconds forever, which is worse than the bug.
        """
        self._impatient(floor=10 ** 9, seconds=0.3)   # nothing can beat this

        with TemporaryDirectory() as tmp:
            got = update.download(self.release, Path(tmp))
            self.assertEqual(got.read_bytes(), PAYLOAD,
                             "a slow download must still complete")

    def test_the_checksum_is_taken_from_the_finished_file(self) -> None:
        """A resumed download never sees the first half, so a running hash
        would be of the wrong bytes and every resume would fail its check."""
        import hashlib

        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "part.bin"
            target.write_bytes(PAYLOAD)
            self.assertEqual(update._digest_of(target),
                             hashlib.sha256(PAYLOAD).hexdigest())


class TidyingUp(unittest.TestCase):
    """914MB of finished downloads was sitting in the data folder."""

    def test_old_archives_go_and_the_current_one_stays(self) -> None:
        with TemporaryDirectory() as tmp:
            into = Path(tmp)
            for name in ("Conrod-0.2.2-win64.zip", "Conrod-0.2.4-win64.zip",
                         "Conrod-0.2.6-win64.zip"):
                (into / name).write_bytes(b"x" * 1000)
            keep = into / "Conrod-0.2.10-win64.zip"
            keep.write_bytes(b"y" * 500)

            freed = update.tidy(into, keep=keep.name)

            self.assertEqual(freed, 3000)
            self.assertTrue(keep.exists())
            self.assertEqual(list(into.glob("Conrod-*.zip")), [keep])

    def test_it_leaves_everything_else_alone(self) -> None:
        with TemporaryDirectory() as tmp:
            into = Path(tmp)
            (into / "swap.ps1").write_text("script")
            (into / "swap.log").write_text("log")
            (into / "Conrod-0.2.2-win64.zip").write_bytes(b"x")

            update.tidy(into)

            self.assertTrue((into / "swap.ps1").exists())
            self.assertTrue((into / "swap.log").exists())
            self.assertEqual(list(into.glob("Conrod-*.zip")), [])


if __name__ == "__main__":
    unittest.main()
