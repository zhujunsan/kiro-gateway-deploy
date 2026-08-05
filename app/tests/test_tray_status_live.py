"""Probe cadence for live status rows while the tray menu is open."""
from __future__ import annotations

import sys
import types

import pytest


def _stub_pystray(monkeypatch):
    if "pystray" in sys.modules:
        return
    mod = types.ModuleType("pystray")

    class _MenuItem:
        def __init__(self, text, action=None, **kwargs):
            self.text = text

    class _Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    mod.MenuItem = _MenuItem
    mod.Menu = _Menu
    monkeypatch.setitem(sys.modules, "pystray", mod)


def _make_app(monkeypatch):
    _stub_pystray(monkeypatch)
    from kiro_gateway_tray.tray import TrayApp

    app = TrayApp()
    app._menu_session_open = True
    return app


def test_repeated_ticks_keep_probing(monkeypatch):
    """A held-open menu must keep re-probing, not probe once and freeze."""
    app = _make_app(monkeypatch)

    probes = {"n": 0}

    def _fake_probe_now():
        probes["n"] += 1
        return False

    monkeypatch.setattr(app.sup, "probe_now", _fake_probe_now)
    # Run the probe worker inline so the throttle gate's busy flag clears
    # deterministically instead of racing a real thread.
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.threading.Thread",
        lambda target, daemon=None: types.SimpleNamespace(start=target),
    )

    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.time.monotonic", lambda: clock["t"]
    )

    # 10 one-second ticks while the menu stays open.
    for _ in range(10):
        app._kick_probe_if_due()
        clock["t"] += 1.0

    # With a 2s gate, 10s of ticks must yield multiple probes -- not just one.
    assert probes["n"] >= 4, f"probe stalled after {probes['n']} run(s)"


def test_probe_gate_does_not_permanently_latch(monkeypatch):
    """A failing probe must still release the gate so later ticks retry."""
    app = _make_app(monkeypatch)

    probes = {"n": 0}

    def _boom():
        probes["n"] += 1
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(app.sup, "probe_now", _boom)
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.threading.Thread",
        lambda target, daemon=None: types.SimpleNamespace(start=target),
    )

    clock = {"t": 500.0}
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.time.monotonic", lambda: clock["t"]
    )

    for _ in range(6):
        app._kick_probe_if_due()
        clock["t"] += 2.0

    assert probes["n"] >= 3, "gate latched after a failing probe"


def test_tick_targets_the_menu_on_screen_not_the_stale_handle(monkeypatch):
    """Ticks must patch the menu menuWillOpen: gave us, not icon._menu_handle.

    pystray only reassigns ``_menu_handle`` inside ``_update_menu``, which we
    suppress while the menu is open, so it can reference an NSMenu that AppKit
    has already replaced. Patching that one updates nothing on screen.
    """
    app = _make_app(monkeypatch)

    stale_menu = object()
    live_menu = object()
    app._icon = types.SimpleNamespace(_menu_handle=(stale_menu, []))

    monkeypatch.setattr(app, "_on_menu_open", lambda: None)
    monkeypatch.setattr(app._activity_cache, "refresh", lambda **k: None)

    seen = []
    monkeypatch.setattr(
        app, "_live_patch_open_menu", lambda snap, nsmenu=None: seen.append(nsmenu)
    )
    app._on_status_menu_will_open(live_menu)
    assert seen == [live_menu]

    # While the session is open, an unqualified resolve must still find the
    # live menu rather than falling back to the stale handle.
    assert app._resolve_status_menu() is live_menu

    app._on_status_menu_did_close(live_menu)
    # After close, fall back to the handle again so post-close redraws work.
    assert app._resolve_status_menu() is stale_menu


def test_status_title_patch_uses_live_menu(monkeypatch):
    """_live_patch_status_titles must follow the same live-menu resolution."""
    app = _make_app(monkeypatch)

    stale_menu = object()
    live_menu = object()
    app._icon = types.SimpleNamespace(_menu_handle=(stale_menu, []))
    app._live_nsmenu = live_menu

    targets = []

    def _find(nsmenu, prefix):
        targets.append(nsmenu)
        return None

    monkeypatch.setattr(
        "kiro_gateway_tray.tray.macos_menu.find_menu_item_by_title_prefix", _find
    )

    app._live_patch_status_titles()

    assert targets, "no lookup happened"
    assert all(t is live_menu for t in targets)


def test_tick_patches_status_titles_every_time(monkeypatch):
    """Each tick must re-read status and re-apply titles (no dedupe skip)."""
    app = _make_app(monkeypatch)
    monkeypatch.setattr(app, "_kick_probe_if_due", lambda: None)

    calls = {"n": 0}
    monkeypatch.setattr(
        app, "_live_patch_status_titles", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    )
    monkeypatch.setattr(app, "_resolve_status_menu", lambda nsmenu=None: object())
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.macos_menu.find_menu_item_by_title_prefix",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.macos_menu.find_menu_item_by_exact_title",
        lambda *a, **k: None,
    )

    from kiro_gateway_tray.request_activity import ActivitySnapshot

    for _ in range(5):
        app._live_patch_open_menu(ActivitySnapshot())

    assert calls["n"] == 5
