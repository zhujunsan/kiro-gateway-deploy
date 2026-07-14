import sys
from types import SimpleNamespace

from kiro_gateway_tray import __main__ as entry


def test_complete_instance_is_not_cleaned_up(monkeypatch):
    shown = []
    cleaned = []
    monkeypatch.setattr(sys, "argv", ["kiro-gateway-tray"])
    monkeypatch.setattr(entry, "_setup_logging", lambda: None)
    monkeypatch.setattr(entry, "_acquire_lock", lambda: False)
    monkeypatch.setattr(entry, "_show_already_running", lambda: shown.append(True))
    monkeypatch.setattr(
        entry.proc_guard, "cleanup_orphans", lambda: cleaned.append(True)
    )

    assert entry.main() == 1
    assert shown == [True]
    assert cleaned == []


def test_orphans_are_cleaned_after_lock_before_cli_start(monkeypatch):
    events = []
    monkeypatch.setattr(sys, "argv", ["kiro-gateway-tray", "--cli"])
    monkeypatch.setattr(entry, "_setup_logging", lambda: None)
    monkeypatch.setattr(
        entry, "_acquire_lock", lambda: events.append("lock") or True
    )
    monkeypatch.setattr(
        entry.proc_guard,
        "cleanup_orphans",
        lambda: events.append("cleanup"),
    )
    fake_cli = SimpleNamespace(run=lambda: events.append("cli") or 0)
    monkeypatch.setitem(sys.modules, "kiro_gateway_tray.cli", fake_cli)
    monkeypatch.setattr(
        sys.modules["kiro_gateway_tray"], "cli", fake_cli, raising=False
    )

    assert entry.main() == 0
    assert events == ["lock", "cleanup", "cli"]
