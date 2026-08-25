# app/tests/test_tray_announcements.py
"""Tests for the announcement rows in the tray menu.

The menu is built once with a fixed number of slots, so the interesting
behaviour is entirely in the per-slot callbacks: which slots claim to be
visible, what they render, whether they are clickable, and where a click goes.
"""
import sys
import types

import pytest

from kiro_gateway_tray import announcements, appconfig
from kiro_gateway_tray.announcements import Announcement
from kiro_gateway_tray import tray as tray_mod


@pytest.fixture(autouse=True)
def _stub_pystray(monkeypatch):
    """TrayApp.__init__ does ``import pystray``; on a headless runner importing
    the real backend raises. Inject a stub so construction works without a GUI."""
    if "pystray" not in sys.modules:
        monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    appconfig.invalidate_cache()


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    """These tests assert announcement rendering, not the GitHub update worker.

    ``_kick_announcement_check`` is deliberately left intact: the row callbacks
    must not trigger a poll on their own, and the tests below rely on that.
    """
    from kiro_gateway_tray.tray import TrayApp

    monkeypatch.setattr(TrayApp, "_kick_update_check", lambda self: None)


def _make_app():
    from kiro_gateway_tray.tray import TrayApp
    return TrayApp()


def _app_with(*items):
    announcements._write_cache(list(items))
    return _make_app()


def _notice(id=1, body="维护通知", **kw):
    return Announcement(id=id, body=body, **kw)


def test_no_announcements_hides_every_slot():
    app = _make_app()
    for i in range(5):
        assert app._announcement_visible(i)(None) is False
        assert app._announcement_line(i)(None) == ""
        assert app._announcement_enabled(i)(None) is False


def test_slots_fill_from_the_top():
    app = _app_with(_notice(id=1, body="第一条"), _notice(id=2, body="第二条"))

    assert app._announcement_visible(0)(None) is True
    assert app._announcement_visible(1)(None) is True
    assert app._announcement_visible(2)(None) is False
    assert app._announcement_line(0)(None) == "📢 第一条"
    assert app._announcement_line(1)(None) == "📢 第二条"


def test_all_five_slots_can_be_filled():
    app = _app_with(*[_notice(id=i + 1, body=f"公告{i}") for i in range(5)])

    assert all(app._announcement_visible(i)(None) for i in range(5))
    assert app._announcement_line(4)(None) == "📢 公告4"


def test_slot_count_matches_the_client_cap():
    from kiro_gateway_tray import tray
    assert tray._ANNOUNCEMENT_SLOTS == announcements.MAX_ITEMS == 5


def test_line_renders_level_emoji_and_gray_tag():
    app = _app_with(_notice(body="紧急维护", level="critical", tag="08-10 截止"))
    assert app._announcement_line(0)(None) == "🚨 紧急维护\t08-10 截止"


def test_linked_announcement_is_clickable_by_default():
    app = _app_with(_notice(url="https://example.com/notice"))
    assert app._announcement_enabled(0)(None) is True


def test_announcement_without_a_link_is_still_enabled_unless_dimmed():
    """Gray is cloud-controlled; missing url alone must not gray the row."""
    app = _app_with(_notice(url=""))
    assert app._announcement_visible(0)(None) is True
    assert app._announcement_enabled(0)(None) is True


def test_dimmed_announcement_is_gray_even_with_a_url():
    app = _app_with(_notice(url="https://example.com/notice", dimmed=True))
    assert app._announcement_visible(0)(None) is True
    assert app._announcement_enabled(0)(None) is False


def test_click_opens_the_url(monkeypatch):
    opened = []
    monkeypatch.setattr("kiro_gateway_tray.tray.webbrowser.open", opened.append)
    app = _app_with(_notice(url="https://example.com/notice"))

    app._on_announcement(0)(None, None)

    assert opened == ["https://example.com/notice"]


def test_click_on_an_unlinked_announcement_does_nothing(monkeypatch):
    opened = []
    monkeypatch.setattr("kiro_gateway_tray.tray.webbrowser.open", opened.append)
    app = _app_with(_notice(url=""))

    app._on_announcement(0)(None, None)

    assert opened == []


def test_click_on_a_dimmed_announcement_does_nothing(monkeypatch):
    opened = []
    monkeypatch.setattr("kiro_gateway_tray.tray.webbrowser.open", opened.append)
    app = _app_with(_notice(url="https://example.com/notice", dimmed=True))

    app._on_announcement(0)(None, None)

    assert opened == []


def test_click_on_an_empty_slot_does_nothing(monkeypatch):
    opened = []
    monkeypatch.setattr("kiro_gateway_tray.tray.webbrowser.open", opened.append)
    app = _make_app()

    app._on_announcement(3)(None, None)

    assert opened == []


def test_expired_cache_entries_never_reach_the_menu():
    """An app left running past ends_at must retire the row on its own."""
    import time
    app = _app_with(
        _notice(id=1, body="已结束", ends_at=int(time.time()) - 60),
        _notice(id=2, body="进行中"),
    )
    assert app._announcement_line(0)(None) == "📢 进行中"
    assert app._announcement_visible(1)(None) is False


def test_cache_is_read_from_disk_only_once(monkeypatch):
    """Menu rendering must not re-read the cache file on every row callback."""
    app = _make_app()
    calls = []
    monkeypatch.setattr(
        announcements, "peek_cached", lambda *a, **k: calls.append(1) or [])

    for i in range(5):
        app._announcement_visible(i)(None)
        app._announcement_line(i)(None)

    assert len(calls) == 1


def test_peek_failure_leaves_the_bar_empty_instead_of_crashing(monkeypatch):
    def _explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(announcements, "peek_cached", _explode)
    app = _make_app()

    assert app._announcement_visible(0)(None) is False
    assert app._announcement_line(0)(None) == ""


def test_menu_open_kicks_a_poll(monkeypatch):
    """The hourly timer lags after sleep; opening the menu nudges it."""
    from kiro_gateway_tray.tray import TrayApp

    kicked = []
    monkeypatch.setattr(TrayApp, "_kick_announcement_check", lambda self: kicked.append(1))
    app = _make_app()

    app._on_menu_open()

    assert kicked == [1]


def test_refresh_interval_is_hourly():
    from kiro_gateway_tray.tray import TrayApp
    assert TrayApp._ANNOUNCEMENT_REFRESH_INTERVAL == 3600


# --- menu placement ---


class _FakeMenuItem:
    """Records what tray.py asked for, without needing a real GUI backend."""

    def __init__(self, text, action=None, enabled=True, visible=True):
        self.text = text
        self.action = action
        self.enabled = enabled
        self.visible = visible

    def title(self):
        return self.text(None) if callable(self.text) else self.text

    def is_visible(self):
        return self.visible(None) if callable(self.visible) else self.visible


class _FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = list(items)


def _built_menu(app):
    fake = types.SimpleNamespace(Menu=_FakeMenu, MenuItem=_FakeMenuItem)
    app._pystray = fake
    return app._build_menu().items


def test_announcements_sit_below_the_update_notice_and_above_the_status_block():
    app = _app_with(_notice(id=1, body="第一条"), _notice(id=2, body="第二条"))
    items = _built_menu(app)

    titles = [i.title() if isinstance(i, _FakeMenuItem) else "---" for i in items]
    update_at = next(i for i, t in enumerate(titles) if t.startswith("🔔") or t == "")
    first_notice = titles.index("📢 第一条")
    gateway_at = next(i for i, t in enumerate(titles) if t.startswith("🖥 网关"))

    assert update_at < first_notice < gateway_at
    assert titles[first_notice + 1] == "📢 第二条"
    # A separator fences the announcements off from the status block.
    assert items[first_notice + announcements.MAX_ITEMS] is _FakeMenu.SEPARATOR


def test_menu_always_reserves_five_slots_even_when_empty():
    """pystray builds the menu once, so unfilled slots must exist and hide."""
    items = _built_menu(_make_app())
    slots = [i for i in items
             if isinstance(i, _FakeMenuItem) and i.title() == "" and not i.is_visible()]
    # Five announcement slots plus the update line, which is also empty+hidden.
    assert len(slots) == announcements.MAX_ITEMS + 1


# --- background poll ---

@pytest.fixture
def _inline_threads(monkeypatch):
    """Run the poll's worker body synchronously so it can be asserted on."""
    class _Inline:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr("kiro_gateway_tray.tray.threading.Thread", _Inline)


def test_poll_redraws_when_the_announcements_change(monkeypatch, _inline_threads):
    app = _make_app()
    redraws = []
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.append(1))
    fresh = [_notice(id=3, body="新公告")]
    monkeypatch.setattr(announcements, "check", lambda *a, **k: fresh)

    app._kick_announcement_check()

    assert app._announcements == fresh
    assert redraws == [1]
    assert app._announcement_line(0)(None) == "📢 新公告"


def test_poll_does_not_redraw_when_nothing_changed(monkeypatch, _inline_threads):
    """Redrawing rebuilds the whole NSMenu; don't do it once an hour for nothing."""
    same = [_notice(id=1, body="没变")]
    app = _app_with(*same)
    app._ensure_announcements_sync()
    redraws = []
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.append(1))
    monkeypatch.setattr(announcements, "check", lambda *a, **k: list(same))

    app._kick_announcement_check()

    assert redraws == []


def test_poll_clears_the_bar_when_everything_is_taken_down(monkeypatch, _inline_threads):
    app = _app_with(_notice(id=1, body="旧公告"))
    app._ensure_announcements_sync()
    redraws = []
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.append(1))
    monkeypatch.setattr(announcements, "check", lambda *a, **k: [])

    app._kick_announcement_check()

    assert app._announcements == []
    assert redraws == [1]
    assert app._announcement_visible(0)(None) is False


def test_poll_swallows_errors_and_releases_the_gate(monkeypatch, _inline_threads):
    app = _make_app()

    def _explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(announcements, "check", _explode)

    app._kick_announcement_check()  # must not raise

    # The gate is released in `finally`, so a later poll still runs.
    ran = []
    monkeypatch.setattr(announcements, "check", lambda *a, **k: ran.append(1) or [])
    app._kick_announcement_check()
    assert ran == [1]


def test_url_changed_notice_is_the_first_visible_row():
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-new.example.com"
    cfg.cloudflare.url_changed_notice = True
    appconfig.save(cfg)

    app = _make_app()
    items = _built_menu(app)
    first_visible = next(
        i for i in items if isinstance(i, _FakeMenuItem) and i.is_visible()
    )
    assert first_visible.title() == tray_mod._URL_CHANGED_TITLE


def test_url_changed_notice_hidden_by_default():
    app = _make_app()
    items = _built_menu(app)
    warning = next(
        i for i in items
        if isinstance(i, _FakeMenuItem) and i.title() == tray_mod._URL_CHANGED_TITLE
    )
    assert warning.is_visible() is False


def test_clicking_url_changed_notice_alerts_then_dismisses(monkeypatch):
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-new.example.com"
    cfg.cloudflare.url_changed_notice = True
    appconfig.save(cfg)

    app = _make_app()
    alerts = []
    monkeypatch.setattr(
        tray_mod.dialogs, "alert",
        lambda title, msg: alerts.append((title, msg)),
    )
    monkeypatch.setattr(app, "_request_redraw", lambda: None)

    app._on_url_changed(None, None)

    assert len(alerts) == 1
    assert alerts[0][0] == "隧道地址已变更"
    assert "kg-new.example.com" in alerts[0][1]
    assert "原先使用旧地址" in alerts[0][1]
    assert appconfig.load().cloudflare.url_changed_notice is False


def test_clicking_url_changed_notice_keeps_prompt_if_alert_fails(monkeypatch):
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-new.example.com"
    cfg.cloudflare.url_changed_notice = True
    appconfig.save(cfg)

    app = _make_app()
    monkeypatch.setattr(
        tray_mod.dialogs, "alert",
        lambda title, msg: (_ for _ in ()).throw(RuntimeError("dialog failed")),
    )
    monkeypatch.setattr(app, "_request_redraw", lambda: None)

    app._on_url_changed(None, None)

    assert appconfig.load().cloudflare.url_changed_notice is True


def test_startup_url_changed_reminder_notifies_without_alert(monkeypatch):
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-new.example.com"
    cfg.cloudflare.url_changed_notice = True
    appconfig.save(cfg)

    app = _make_app()
    notes = []
    alerts = []
    monkeypatch.setattr(app, "_notify", lambda title, msg: notes.append((title, msg)))
    monkeypatch.setattr(tray_mod.dialogs, "alert", lambda title, msg: alerts.append((title, msg)))

    app._notify_url_changed_if_needed()

    assert notes and "隧道地址已变更" in notes[0][1]
    assert alerts == []
    assert appconfig.load().cloudflare.url_changed_notice is True
