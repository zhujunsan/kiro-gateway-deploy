# app/tests/test_platform_compat.py
import ctypes

from kiro_gateway_tray import platform_compat
from kiro_gateway_tray.platform_compat import SingleInstanceLock


def test_single_instance_lock_blocks_second_acquire(tmp_path):
    lock_path = tmp_path / "test.lock"
    a = SingleInstanceLock(lock_path)
    assert a.acquire() is True

    b = SingleInstanceLock(lock_path)
    assert b.acquire() is False  # held by `a`


def test_single_instance_lock_reacquire_after_release(tmp_path):
    lock_path = tmp_path / "test.lock"
    a = SingleInstanceLock(lock_path)
    assert a.acquire() is True
    # drop the handle to release the OS lock
    a._fd.close()

    b = SingleInstanceLock(lock_path)
    assert b.acquire() is True


def test_copy_to_clipboard_win32_uses_native_unicode_clipboard(monkeypatch):
    monkeypatch.setattr(platform_compat.sys, "platform", "win32")

    def _run_should_not_be_needed(*_args, **_kwargs):
        raise AssertionError("Windows clipboard copy should not shell out to clip.exe")

    monkeypatch.setattr(platform_compat.subprocess, "run", _run_should_not_be_needed)

    calls = []
    moved = {}

    class _FakeFunction:
        def __init__(self, name, impl):
            self.name = name
            self.impl = impl
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.impl(*args)

    class _FakeKernel32:
        def __init__(self):
            self.GlobalAlloc = _FakeFunction("GlobalAlloc", lambda _flags, _size: 100)
            self.GlobalLock = _FakeFunction("GlobalLock", lambda _handle: 200)
            self.GlobalUnlock = _FakeFunction("GlobalUnlock", lambda _handle: 1)
            self.GlobalFree = _FakeFunction("GlobalFree", lambda _handle: 0)

    class _FakeUser32:
        def __init__(self):
            self.OpenClipboard = _FakeFunction("OpenClipboard", lambda _owner: 1)
            self.EmptyClipboard = _FakeFunction("EmptyClipboard", lambda: 1)
            self.SetClipboardData = _FakeFunction("SetClipboardData", lambda _fmt, handle: handle)
            self.CloseClipboard = _FakeFunction("CloseClipboard", lambda: 1)

    def _fake_windll(name, use_last_error=True):
        assert use_last_error is True
        if name == "kernel32":
            return _FakeKernel32()
        if name == "user32":
            return _FakeUser32()
        raise AssertionError(f"unexpected DLL: {name}")

    def _fake_memmove(dest, src, size):
        moved["dest"] = dest
        moved["src"] = src
        moved["size"] = size

    monkeypatch.setattr(ctypes, "WinDLL", _fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "memmove", _fake_memmove)

    platform_compat.copy_to_clipboard("隧道 URL")

    assert moved == {
        "dest": 200,
        "src": "隧道 URL".encode("utf-16le") + b"\x00\x00",
        "size": len("隧道 URL".encode("utf-16le") + b"\x00\x00"),
    }
    assert ("OpenClipboard", (None,)) in calls
    assert ("EmptyClipboard", ()) in calls
    assert ("SetClipboardData", (13, 100)) in calls
    assert ("CloseClipboard", ()) in calls
