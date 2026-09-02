"""Native Windows dialogs, without a UI toolkit.

The app's window is a browser window (see desktop.py), so there is no
toolkit to ask for a folder picker. Rather than pull in tkinter or Qt for one
dialog, this calls the same COM interface Explorer uses. It works no matter
how the UI is being shown — app window, or a plain browser tab, which
previously had no picker at all.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from ctypes.wintypes import DWORD, HWND, LPCWSTR

if sys.platform == "win32":
    from ctypes import HRESULT, OleDLL, WinDLL

    ole32 = OleDLL("ole32")
    shell32 = WinDLL("shell32")
else:  # pragma: no cover - the app is Windows-only
    ole32 = shell32 = None


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ole32.CLSIDFromString(text, byref(self))


CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"

CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 0x2
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
SIGDN_FILESYSPATH = 0x80058000

# vtable slots, counting from IUnknown. IFileOpenDialog inherits
# IFileDialog : IModalWindow : IUnknown, so the offsets are fixed.
_RELEASE = 2
_SHOW = 3
_SET_OPTIONS = 9
_SET_TITLE = 17
_GET_RESULT = 20
_ITEM_GET_DISPLAY_NAME = 5


def _call(interface: c_void_p, slot: int, restype, argtypes, *args):
    vtable = ctypes.cast(interface, POINTER(POINTER(c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[slot])
    return fn(interface, *args)


def pick_folder(title: str = "Choose a folder of frames") -> str | None:
    """Show the standard folder picker. None if the user cancelled.

    Raises OSError only when the dialog cannot be created at all, which the
    caller should report rather than silently swallow — a picker that does
    nothing when clicked is worse than one that says why.
    """
    if sys.platform != "win32":
        return None

    # The dialog is apartment-threaded and this may be any worker thread.
    # RPC_E_CHANGED_MODE means COM is already up in another mode, which is
    # fine — we just must not uninitialise it on the way out.
    initialised = True
    try:
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    except OSError:
        initialised = False

    dialog = c_void_p()
    try:
        ole32.CoCreateInstance(
            byref(_GUID(CLSID_FileOpenDialog)), None, CLSCTX_INPROC_SERVER,
            byref(_GUID(IID_IFileOpenDialog)), byref(dialog),
        )
        _call(dialog, _SET_OPTIONS, ctypes.HRESULT, [DWORD],
              DWORD(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM))
        _call(dialog, _SET_TITLE, ctypes.HRESULT, [LPCWSTR], title)

        # S_OK means a choice; anything else (including cancel) means none.
        hresult = _call(dialog, _SHOW, ctypes.c_long, [HWND], None)
        if hresult != 0:
            return None

        item = c_void_p()
        _call(dialog, _GET_RESULT, ctypes.HRESULT, [POINTER(c_void_p)],
              byref(item))
        try:
            path = c_wchar_p()
            _call(item, _ITEM_GET_DISPLAY_NAME, ctypes.HRESULT,
                  [DWORD, POINTER(c_wchar_p)], DWORD(SIGDN_FILESYSPATH),
                  byref(path))
            chosen = path.value
            if path:
                ole32.CoTaskMemFree(path)
            return chosen
        finally:
            _call(item, _RELEASE, ctypes.c_ulong, [])
    finally:
        if dialog:
            _call(dialog, _RELEASE, ctypes.c_ulong, [])
        if initialised:
            ole32.CoUninitialize()
