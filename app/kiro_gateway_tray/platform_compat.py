"""Platform-specific OS integration, isolated behind a small cross-platform API.

Keeping ``sys.platform`` branches here (instead of sprinkled through tray.py)
makes the behavior testable and the UI code platform-agnostic.
"""
from __future__ import annotations

import subprocess
import sys


def copy_to_clipboard(value: str) -> None:
    """Copy text to the OS clipboard. Raises on failure so callers can fall back."""
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=value.encode(), check=True, timeout=5)
    elif sys.platform == "win32":
        # clip expects UTF-16LE on Windows.
        subprocess.run(["clip"], input=value.encode("utf-16le"), check=True, timeout=5)
    else:
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=value.encode(), check=True, timeout=5,
        )


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
