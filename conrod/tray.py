r"""The notification area icon, so closing the window does not end the job.

Conrod's window is a browser window (see desktop.py), and the process used to
live exactly as long as it. That is the wrong lifetime for this app: a scan
runs for hours, the window is the least interesting part of it, and closing
the window to get it off the screen threw the work away.

So the window and the program are now separate things. Closing the window
leaves Conrod running with an icon in the notification area, and the scan
carries on.

There is no toolkit here for the same reason nativeui.py has none -- the UI
is a web page, and pulling in Qt or tkinter to draw one 16-pixel icon would
be far more machinery than the job needs, with more to go wrong in a frozen
build. This is the Win32 shell API through ctypes, which is what a tray icon
is underneath anyway.

The message loop must run on the thread that created the window, so all of
this lives on a thread of its own and the callbacks are handed back out to
another one -- a menu click that opened a browser on the message loop would
freeze the icon until the browser had started.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path

if sys.platform == "win32":
    from ctypes import WINFUNCTYPE, byref, sizeof
    from ctypes.wintypes import (ATOM, BOOL, DWORD, HBRUSH, HICON, HINSTANCE,
                                 HMENU, HWND, LPARAM, LPCWSTR, MSG, POINT,
                                 UINT, WPARAM)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)
else:  # pragma: no cover - the app is Windows-only
    user32 = shell32 = kernel32 = None
    WNDPROC = None


NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_NONE = 0x00

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1            # our icon's callback
WM_STOP = WM_APP + 2            # asking the loop to end

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000
IDI_APPLICATION = 32512
HWND_MESSAGE = -3

ID_OPEN, ID_QUIT = 1001, 1002


if sys.platform == "win32":

    class WNDCLASSEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", UINT), ("style", UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", HINSTANCE), ("hIcon", HICON),
            ("hCursor", ctypes.c_void_p), ("hbrBackground", HBRUSH),
            ("lpszMenuName", LPCWSTR), ("lpszClassName", LPCWSTR),
            ("hIconSm", HICON)]

    class NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT), ("uFlags", UINT),
            ("uCallbackMessage", UINT), ("hIcon", HICON),
            ("szTip", ctypes.c_wchar * 128),
            ("dwState", DWORD), ("dwStateMask", DWORD),
            ("szInfo", ctypes.c_wchar * 256),
            ("uVersion", UINT),
            ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", HICON)]

    # ctypes returns c_int by default, which truncates a 64-bit handle to its
    # low 32 bits -- the same trap nativeui.py documents. Every call that
    # returns or takes a handle is declared.
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    user32.RegisterClassExW.restype = ATOM
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
    user32.CreateWindowExW.restype = HWND
    user32.CreateWindowExW.argtypes = [
        DWORD, LPCWSTR, LPCWSTR, DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, HWND, HMENU, HINSTANCE, ctypes.c_void_p]
    user32.DestroyWindow.argtypes = [HWND]
    user32.LoadImageW.restype = HICON
    user32.LoadImageW.argtypes = [HINSTANCE, LPCWSTR, UINT, ctypes.c_int,
                                  ctypes.c_int, UINT]
    user32.LoadIconW.restype = HICON
    # The second argument is either a string or MAKEINTRESOURCE(n) -- a small
    # integer cast to a pointer -- so it cannot be declared LPCWSTR.
    user32.LoadIconW.argtypes = [HINSTANCE, ctypes.c_void_p]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.DispatchMessageW.restype = LRESULT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.CreatePopupMenu.restype = HMENU
    user32.AppendMenuW.argtypes = [HMENU, UINT, ctypes.c_size_t, LPCWSTR]
    user32.TrackPopupMenu.restype = BOOL
    user32.TrackPopupMenu.argtypes = [HMENU, UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, HWND, ctypes.c_void_p]
    user32.DestroyMenu.argtypes = [HMENU]
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), HWND, UINT, UINT]
    user32.RegisterWindowMessageW.restype = UINT
    user32.RegisterWindowMessageW.argtypes = [LPCWSTR]
    shell32.Shell_NotifyIconW.restype = BOOL
    shell32.Shell_NotifyIconW.argtypes = [DWORD, ctypes.POINTER(NOTIFYICONDATA)]
    kernel32.GetModuleHandleW.restype = HINSTANCE
    kernel32.GetModuleHandleW.argtypes = [LPCWSTR]


def icon_path() -> Path | None:
    """The .ico that ships with the app, if it is there."""
    from .config import bundle_dir

    candidate = bundle_dir() / "assets" / "conrod.ico"
    return candidate if candidate.is_file() else None


def available() -> bool:
    return sys.platform == "win32"


class Tray:
    """One notification-area icon, running its own message loop.

    ``on_open`` and ``on_quit`` are called on a worker thread, never on the
    message loop, so they may take as long as they need.
    """

    def __init__(self, on_open, on_quit, tooltip: str = "Conrod") -> None:
        self.on_open = on_open
        self.on_quit = on_quit
        self.tooltip = tooltip
        self.hwnd = None
        self._icon = None
        self._data = None
        self._thread = None
        self._ready = threading.Event()
        self._added = False
        # Windows keeps a raw pointer to the callback. Letting Python collect
        # it leaves the shell calling into freed memory the next time anyone
        # moves the mouse over the icon.
        self._wndproc = WNDPROC(self._on_message) if WNDPROC else None
        self._taskbar_created = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        """Show the icon. False if this platform or this desktop has none."""
        if not available():
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="conrod-tray")
        self._thread.start()
        self._ready.wait(timeout)
        return self._added

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_STOP, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # -- the icon ----------------------------------------------------------

    def _load_icon(self):
        path = icon_path()
        if path is not None:
            handle = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
        # No icon file is not a reason to have no tray icon.
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _describe(self, flags: int) -> NOTIFYICONDATA:
        data = NOTIFYICONDATA()
        data.cbSize = sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icon or 0
        data.szTip = self.tooltip[:127]
        return data

    def _add(self) -> bool:
        data = self._describe(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        return bool(shell32.Shell_NotifyIconW(NIM_ADD, byref(data)))

    def set_tooltip(self, text: str) -> None:
        """What the icon says on hover -- a scan's progress, usually."""
        self.tooltip = text or "Conrod"
        if not self._added:
            return
        data = self._describe(NIF_TIP)
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(data))

    def notify(self, title: str, message: str) -> None:
        """A balloon, so a window that vanished into the tray says where."""
        if not self._added:
            return
        data = self._describe(NIF_INFO)
        data.szInfo = message[:255]
        data.szInfoTitle = title[:63]
        data.dwInfoFlags = NIIF_NONE
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(data))

    # -- message loop ------------------------------------------------------

    def _run(self) -> None:
        try:
            self._make_window()
            self._icon = self._load_icon()
            self._added = self._add()
        except Exception:
            self._added = False
        finally:
            self._ready.set()

        if not self._added:
            return

        msg = MSG()
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

    def _make_window(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        cls = WNDCLASSEX()
        cls.cbSize = sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = instance
        cls.lpszClassName = f"ConrodTray{id(self):x}"
        if not user32.RegisterClassExW(byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._class = cls          # keep the class (and its name) alive

        # A message-only window: never shown, never in the taskbar, and it
        # exists purely to receive the icon's clicks.
        self.hwnd = user32.CreateWindowExW(
            0, cls.lpszClassName, "Conrod", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    def _later(self, fn) -> None:
        """Run a callback off the message loop."""
        threading.Thread(target=fn, daemon=True).start()

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._later(self.on_open)
            elif event == WM_RBUTTONUP:
                self._show_menu()
            return 0

        if message == WM_STOP:
            user32.DestroyWindow(hwnd)
            return 0

        if message == WM_DESTROY:
            data = self._describe(0)
            shell32.Shell_NotifyIconW(NIM_DELETE, byref(data))
            self._added = False
            user32.PostQuitMessage(0)
            return 0

        # Explorer restarting takes every tray icon with it, and an app that
        # does not put its own back simply disappears until it is restarted.
        if self._taskbar_created and message == self._taskbar_created:
            self._added = self._add()
            return 0

        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open Conrod")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit Conrod")

        point = POINT()
        user32.GetCursorPos(byref(point))
        # Without this the menu will not close when clicked away from, which
        # is a documented quirk of tray menus rather than a guess.
        user32.SetForegroundWindow(self.hwnd)
        choice = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
            point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)      # WM_NULL, same reason
        user32.DestroyMenu(menu)

        if choice == ID_OPEN:
            self._later(self.on_open)
        elif choice == ID_QUIT:
            self._later(self.on_quit)
