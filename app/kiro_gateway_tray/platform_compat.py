"""Platform-specific OS integration, isolated behind a small cross-platform API.

Keeping ``sys.platform`` branches here (instead of sprinkled through tray.py)
makes the behavior testable and the UI code platform-agnostic.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes


def open_file(path) -> None:
    """Open a file with the default application."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        subprocess.run(["start", "", str(path)], shell=True, check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def open_directory(path) -> None:
    """Open a directory in the system file manager."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def copy_to_clipboard(value: str) -> None:
    """Copy text to the OS clipboard. Raises on failure so callers can fall back."""
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=value.encode(), check=True, timeout=5)
    elif sys.platform == "win32":
        _copy_to_clipboard_win32(value)
    else:
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=value.encode(), check=True, timeout=5,
        )


def _win_error(message: str) -> OSError:
    err = ctypes.get_last_error()
    win_error = getattr(ctypes, "WinError", None)
    if err and win_error is not None:
        return win_error(err)
    return OSError(message)


def _copy_to_clipboard_win32(value: str) -> None:
    """Write Unicode text directly to the Windows clipboard."""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    data = value.encode("utf-16le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise _win_error("GlobalAlloc failed")

    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise _win_error("GlobalLock failed")
    try:
        ctypes.memmove(locked, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise _win_error("OpenClipboard failed")

    try:
        if not user32.EmptyClipboard():
            raise _win_error("EmptyClipboard failed")
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise _win_error("SetClipboardData failed")
        handle = None  # Clipboard owns the memory after SetClipboardData succeeds.
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


class SingleInstanceLock:
    """Best-effort cross-platform single-instance file lock.

    Holds the file handle for the process lifetime; the OS releases the lock on
    exit. Returns False from ``acquire`` if another instance already holds it.
    """

    def __init__(self, lock_path) -> None:
        self._path = lock_path
        self._fd = None

    def acquire(self) -> bool:
        try:
            self._fd = open(self._path, "w")
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        import os
        self._fd.write(str(os.getpid()))
        self._fd.flush()
        return True
