# app/kiro_gateway_tray/proc_guard.py
"""Keep the cloudflared child from outliving the tray as an orphan.

The tray launches cloudflared with a bare ``subprocess.Popen``. If the tray is
hard-killed or crashes (``kill -9``, a panic, logout) its normal
``Supervisor.stop()`` never runs, so cloudflared is reparented to launchd/init
and keeps running — still holding its metrics port. On the next launch the new
cloudflared cannot bind that port; cloudflared treats a failed metrics bind as
fatal and exits, so the tunnel silently dies.

This module provides three layers of defense, used together:

1. **OS-level "parent dies -> child dies" binding** (``spawn_kwargs`` +
   ``after_spawn``): a Windows Job Object with KILL_ON_JOB_CLOSE, and Linux
   PR_SET_PDEATHSIG. macOS has no equivalent primitive, so it relies on layers
   2/3.
2. **PID file** (``record_pid`` / ``read_pid`` / ``clear_pid``): the spawned
   cloudflared PID is persisted so a later launch can find a survivor.
3. **Startup sweep** (``kill_orphan``): before starting a new cloudflared, kill
   any recorded PID that is still alive *and* actually a cloudflared process
   (guards against PID reuse).
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from . import paths
from .log import logger

_PID_FILENAME = "cloudflared.pid"


def _pid_file() -> Path:
    return paths.data_dir() / _PID_FILENAME


def record_pid(pid: int) -> None:
    """Persist the running cloudflared PID so a later launch can reap an orphan."""
    try:
        paths.ensure_dirs()
        _pid_file().write_text(str(pid), encoding="utf-8")
    except OSError:
        logger.debug("could not write cloudflared pid file", exc_info=True)


def read_pid() -> int | None:
    try:
        return int(_pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_pid() -> None:
    try:
        _pid_file().unlink()
    except OSError:
        pass


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else; treat as alive (don't try to kill).
        return True
    return True


def _looks_like_cloudflared(pid: int) -> bool:
    """Best-effort check that ``pid`` is actually a cloudflared process.

    PIDs are reused, so before killing a recorded PID we confirm the image name
    still matches. Falls back to True only when we cannot inspect (so a real
    orphan is not left running just because inspection is unavailable)."""
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            import subprocess
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode != 0:
                return False
            return "cloudflared" in out.stdout.lower()
        if sys.platform == "win32":
            return _win_image_is_cloudflared(pid)
    except Exception:
        logger.debug("cloudflared identity check failed for pid {}", pid, exc_info=True)
    return True


def kill_orphan() -> bool:
    """Kill a previously-recorded cloudflared that survived the last session.

    Returns True if an orphan was found and signalled. Only kills when the PID
    is alive AND still maps to a cloudflared image (PID-reuse guard)."""
    pid = read_pid()
    if pid is None:
        return False
    if not _pid_is_alive(pid):
        clear_pid()
        return False
    if not _looks_like_cloudflared(pid):
        clear_pid()
        return False
    logger.warning("found orphaned cloudflared (pid {}); terminating", pid)
    _terminate(pid)
    clear_pid()
    return True


def _terminate(pid: int) -> None:
    if sys.platform == "win32":
        _win_terminate(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.warning("not permitted to terminate pid {}", pid)
        return
    # Give it a moment to go down gracefully, then SIGKILL if still alive.
    import time
    for _ in range(20):  # up to ~2s
        if not _pid_is_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --- Layer 1: OS-level parent->child lifetime binding -----------------------

def spawn_kwargs() -> dict:
    """Extra ``subprocess.Popen`` kwargs that help bind the child's lifetime.

    Linux: install PR_SET_PDEATHSIG in a preexec_fn so the kernel sends SIGTERM
    to cloudflared the moment the tray dies, even on SIGKILL of the tray.
    Windows: create the process in a new, suspended-free group so it can be put
    in a Job Object by ``after_spawn``. macOS: nothing (no primitive)."""
    if sys.platform.startswith("linux"):
        return {"preexec_fn": _linux_set_pdeathsig}
    if sys.platform == "win32":
        return {"creationflags": _win_creationflags()}
    return {}


def after_spawn(proc) -> None:
    """Post-spawn binding that can't be expressed as Popen kwargs.

    Windows: assign the child to a kill-on-close Job Object so the OS reaps it
    when the tray exits for any reason. Other platforms: no-op."""
    if sys.platform == "win32":
        _win_assign_job(proc)


def _linux_set_pdeathsig() -> None:  # pragma: no cover - runs in child, Linux-only
    """Ask the kernel to SIGTERM us when our parent dies. Runs in the child
    between fork and exec."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:
        # Best effort; layers 2/3 still cover us.
        pass


# --- Windows Job Object plumbing -------------------------------------------
# Implemented with ctypes so we add no dependency. All wrapped in try/except so
# a non-Windows import never fails and a Windows API hiccup never blocks launch.

def _win_creationflags() -> int:  # pragma: no cover - Windows-only
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    return CREATE_NEW_PROCESS_GROUP


_WIN_JOB = None  # keep a process-wide handle alive; closing it kills the job


def _win_assign_job(proc) -> None:  # pragma: no cover - Windows-only
    global _WIN_JOB
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return
        h_proc = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid
        )
        if not h_proc:
            kernel32.CloseHandle(job)
            return
        ok = kernel32.AssignProcessToJobObject(job, h_proc)
        kernel32.CloseHandle(h_proc)
        if not ok:
            kernel32.CloseHandle(job)
            return
        # Hold the handle for the tray's lifetime; when the tray exits, the
        # handle closes and the OS kills everything in the job.
        _WIN_JOB = job
    except Exception:
        logger.debug("could not assign cloudflared to a Job Object", exc_info=True)


def _win_pid_is_alive(pid: int) -> bool:  # pragma: no cover - Windows-only
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False


def _win_image_is_cloudflared(pid: int) -> bool:  # pragma: no cover - Windows-only
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return "cloudflared" in out.stdout.lower()
    except Exception:
        return True


def _win_terminate(pid: int) -> None:  # pragma: no cover - Windows-only
    try:
        import subprocess
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=10,
        )
    except Exception:
        logger.debug("taskkill failed for pid {}", pid, exc_info=True)
