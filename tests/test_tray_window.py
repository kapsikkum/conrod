"""Closing the window must not end a scan that is hours into its work.

The process used to live exactly as long as its browser window: `wait()`
returned and main() fell off the end. Closing the window to get it off the
screen therefore threw away however many hours of scanning were in progress,
which is the wrong lifetime for this program.

The window is now something Conrod has rather than something Conrod is.
These tests drive that lifetime with a stand-in for the browser, because the
real one shares a profile directory with any Conrod already running and the
orphan cleanup would reach across and close it.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from conrod import desktop
from conrod.config import Settings


class FakeBrowser:
    """Enough of subprocess.Popen for AppWindow: poll, wait, terminate."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.terminated = False

    def poll(self):
        return 0 if self._closed.is_set() else None

    def wait(self, timeout=None):
        self._closed.wait(timeout)
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self._closed.set()

    # what the person at the keyboard does
    close = terminate


class NoRealCleanup(unittest.TestCase):
    """Keep the orphan-window sweep out of these tests.

    A window that dies within five seconds of opening is normally Chromium
    handing the URL to an existing profile, so AppWindow sweeps up the stale
    browser. A stand-in window closes in microseconds, which trips that every
    time and shells out to PowerShell -- slow enough on a CI runner to fail
    the timeouts, and it would be reaching for real browsers besides.
    """

    def setUp(self) -> None:
        sweep = mock.patch.object(desktop, "_close_orphan_windows",
                                  return_value=False)
        self.sweep = sweep.start()
        self.addCleanup(sweep.stop)


class TheWindow(NoRealCleanup):
    def test_it_can_be_opened_again_after_being_closed(self) -> None:
        """The whole point. The old code had no way to do this."""
        windows = [FakeBrowser(), FakeBrowser()]
        with mock.patch.object(desktop, "_open_app_window",
                               side_effect=windows):
            window = desktop.AppWindow("http://127.0.0.1:1")
            self.assertTrue(window.open())
            self.assertTrue(window.is_open())

            windows[0].close()
            window.wait()
            self.assertFalse(window.is_open())

            self.assertTrue(window.open())
            self.assertTrue(window.is_open())

    def test_opening_twice_does_not_open_two_windows(self) -> None:
        opened = []

        def once(url):
            opened.append(url)
            return FakeBrowser()

        with mock.patch.object(desktop, "_open_app_window", side_effect=once):
            window = desktop.AppWindow("http://127.0.0.1:1")
            window.open()
            window.open()
        self.assertEqual(len(opened), 1)

    def test_a_window_we_closed_is_not_mistaken_for_a_handoff(self) -> None:
        """Chromium hands a URL to an existing profile and exits at once, and
        a window that dies inside five seconds is normally that. But when
        Conrod closed it deliberately -- to install an update -- running the
        cleanup would kill browsers and cost seconds during a shutdown that
        is being waited on."""
        browser = FakeBrowser()
        with mock.patch.object(desktop, "_open_app_window",
                               return_value=browser), \
             mock.patch.object(desktop, "_close_orphan_windows") as cleanup:
            window = desktop.AppWindow("http://127.0.0.1:1")
            window.open()
            window.terminate()
            window.wait()
        cleanup.assert_not_called()


class StayingAlive(NoRealCleanup):
    """_run_windowed: the loop that decides whether closing means quitting."""

    def _tray(self):
        """A stand-in for the notification area that records what it shows."""
        icon = mock.Mock()
        icon.start.return_value = True
        return icon

    def test_closing_the_window_leaves_conrod_running(self) -> None:
        browser, second = FakeBrowser(), FakeBrowser()
        icon = self._tray()
        settings = Settings()
        settings.close_to_tray = True

        with mock.patch.object(desktop, "_open_app_window",
                               side_effect=[browser, second]), \
             mock.patch.object(desktop.tray, "available", return_value=True), \
             mock.patch.object(desktop.tray, "Tray", return_value=icon), \
             mock.patch.object(desktop.server, "set_quit_hook") as hook:

            done = threading.Event()
            threading.Thread(
                target=lambda: (desktop._run_windowed("http://127.0.0.1:1",
                                                      settings), done.set()),
                daemon=True).start()

            # The person closes the window. Wait for the balloon rather than
            # for a fixed moment -- a fixed one passes alone and fails in the
            # full suite, which is the worst way to learn about a race.
            browser.close()
            deadline = time.monotonic() + 5.0
            while not icon.notify.called and time.monotonic() < deadline:
                time.sleep(0.02)

            self.assertFalse(done.is_set(),
                             "closing the window ended the program")
            icon.notify.assert_called_once()   # it said where it went, once

            # Quitting from the tray is what actually ends it.
            shutdown = hook.call_args[0][0]
            shutdown()
            self.assertTrue(done.wait(3.0), "quitting from the tray did not")

    def test_without_the_setting_closing_the_window_still_quits(self) -> None:
        browser = FakeBrowser()
        settings = Settings()
        settings.close_to_tray = False

        with mock.patch.object(desktop, "_open_app_window",
                               return_value=browser), \
             mock.patch.object(desktop.tray, "Tray") as tray_class, \
             mock.patch.object(desktop.server, "set_quit_hook"):

            done = threading.Event()
            threading.Thread(
                target=lambda: (desktop._run_windowed("http://127.0.0.1:1",
                                                      settings), done.set()),
                daemon=True).start()
            browser.close()
            self.assertTrue(done.wait(3.0))
        tray_class.assert_not_called()

    def test_the_updater_quit_ends_the_process_rather_than_hiding(self) -> None:
        """Installing replaces the folder this exe runs from, which Windows
        refuses while it is open. A quit that went to the tray would leave the
        swap script waiting on a process that never exits -- the exact failure
        the quit hook was added to fix."""
        browser = FakeBrowser()
        icon = self._tray()
        settings = Settings()
        settings.close_to_tray = True

        with mock.patch.object(desktop, "_open_app_window",
                               return_value=browser), \
             mock.patch.object(desktop.tray, "available", return_value=True), \
             mock.patch.object(desktop.tray, "Tray", return_value=icon), \
             mock.patch.object(desktop.server, "set_quit_hook") as hook:

            done = threading.Event()
            threading.Thread(
                target=lambda: (desktop._run_windowed("http://127.0.0.1:1",
                                                      settings), done.set()),
                daemon=True).start()
            threading.Event().wait(0.2)

            shutdown = hook.call_args[0][0]
            shutdown()

            self.assertTrue(done.wait(3.0), "the update would have hung here")
            self.assertTrue(browser.terminated, "the window must be closed")
            icon.stop.assert_called_once()

    def test_no_window_at_all_falls_back_to_the_browser(self) -> None:
        with mock.patch.object(desktop, "_open_app_window", return_value=None):
            self.assertFalse(
                desktop._run_windowed("http://127.0.0.1:1", Settings()))


class TheIcon(unittest.TestCase):
    def test_the_app_ships_one(self) -> None:
        """The spec's icon= line was guarded by an exists() check that had
        never once been true, so the exe wore PyInstaller's own feather."""
        from conrod import tray

        path = tray.icon_path()
        self.assertIsNotNone(path, "assets/conrod.ico is missing")
        self.assertGreater(path.stat().st_size, 1000)

    def test_it_is_reachable_from_a_frozen_build(self) -> None:
        """It is loaded from disk at runtime, so being the exe's icon is not
        enough -- it has to be in the spec's datas as well."""
        from pathlib import Path

        spec = Path(__file__).resolve().parent.parent / "conrod.spec"
        self.assertIn("conrod.ico", spec.read_text(encoding="utf-8"))

    def test_the_setting_is_on_the_settings_screen(self) -> None:
        import conrod
        from pathlib import Path

        source = (Path(conrod.__file__).parent / "web" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn('"close_to_tray"', source)


if __name__ == "__main__":
    unittest.main()
