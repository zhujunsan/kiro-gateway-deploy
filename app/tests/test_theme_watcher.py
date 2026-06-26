# app/tests/test_theme_watcher.py
"""Platform-safe tests for theme-aware icon + ThemeWatcher.

The Win32 registry-notify loop can only run on Windows, so here we cover the
pieces that are testable everywhere: the registry-read helper's error/default
behavior (mocked) and the off-Windows no-op contract of ThemeWatcher.start().
"""
import sys

import pytest

from kiro_gateway_tray import icon as icon_mod
from kiro_gateway_tray.theme_watcher import ThemeWatcher


def test_windows_uses_light_theme_false_off_windows(monkeypatch):
    # On non-Windows it must short-circuit to False without touching winreg.
    monkeypatch.setattr(icon_mod.sys, "platform", "darwin")
    assert icon_mod.windows_uses_light_theme() is False


def test_windows_uses_light_theme_reads_registry(monkeypatch):
    """Simulate the win32 branch by faking sys.platform and a winreg module."""
    monkeypatch.setattr(icon_mod.sys, "platform", "win32")

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_winreg = type(sys)("winreg")
    fake_winreg.HKEY_CURRENT_USER = 0x80000001

    def _open_key(root, sub):
        assert "Personalize" in sub
        return _FakeKey()

    def _query(key, name):
        assert name == "SystemUsesLightTheme"
        return (1, 4)

    fake_winreg.OpenKey = _open_key
    fake_winreg.QueryValueEx = _query
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert icon_mod.windows_uses_light_theme() is True


def test_windows_uses_light_theme_missing_key_defaults_false(monkeypatch):
    monkeypatch.setattr(icon_mod.sys, "platform", "win32")
    fake_winreg = type(sys)("winreg")
    fake_winreg.HKEY_CURRENT_USER = 0x80000001

    def _open_key(root, sub):
        raise FileNotFoundError("missing")

    fake_winreg.OpenKey = _open_key
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert icon_mod.windows_uses_light_theme() is False


@pytest.mark.skipif(sys.platform == "win32", reason="no-op contract is off-Windows only")
def test_theme_watcher_start_is_noop_off_windows():
    fired = []
    w = ThemeWatcher(lambda v: fired.append(v))
    w.start()
    # No thread spawned, no callback.
    assert w._thread is None
    w.stop()
    assert fired == []


def test_make_icon_renders_both_themes():
    # Both colorings render without error and produce an image of expected size.
    light = icon_mod._make_icon_solid(True, light_theme=True)
    dark = icon_mod._make_icon_solid(False, light_theme=False)
    assert light.size == dark.size
