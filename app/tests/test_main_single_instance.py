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


def test_lock_failure_never_starts_tray(monkeypatch):
    """Single-instance lock is acquired before any network recovery or start."""
    events = []
    monkeypatch.setattr(sys, "argv", ["kiro-gateway-tray"])
    monkeypatch.setattr(entry, "_setup_logging", lambda: None)
    monkeypatch.setattr(entry, "_acquire_lock", lambda: False)
    monkeypatch.setattr(entry, "_show_already_running", lambda: events.append("shown"))
    monkeypatch.setattr(
        entry.proc_guard, "cleanup_orphans", lambda: events.append("cleanup")
    )

    class _Boom:
        def run(self):
            events.append("tray")
            raise AssertionError("tray must not start")

    monkeypatch.setitem(sys.modules, "kiro_gateway_tray.tray", _Boom())
    assert entry.main() == 1
    assert events == ["shown"]


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


# --- multiprocessing helper diversion (freeze_support) -------------------------

import multiprocessing
import subprocess
from pathlib import Path

import pytest

_TRACKER_ARGV = [
    "KiroGatewayTray", "-B", "-S", "-I", "-c",
    "from multiprocessing.resource_tracker import main;main(8)",
]


def _install_divert_spy(monkeypatch, events, *, exit_on_tracker=False):
    """Replace multiprocessing.freeze_support with a recording stand-in.

    With ``exit_on_tracker`` it mimics PyInstaller's runtime hook: helper
    argv is executed elsewhere and the process exits before argparse.
    """

    def _freeze_support():
        events.append("freeze_support")
        if exit_on_tracker and "-c" in sys.argv:
            raise SystemExit(0)

    monkeypatch.setattr(multiprocessing, "freeze_support", _freeze_support)


def test_freeze_support_runs_before_argparse(monkeypatch):
    events = []
    _install_divert_spy(monkeypatch, events)
    real_parse = entry.argparse.ArgumentParser.parse_args

    def _parse(self, *args, **kwargs):
        events.append("parse_args")
        return real_parse(self, *args, **kwargs)

    monkeypatch.setattr(entry.argparse.ArgumentParser, "parse_args", _parse)
    monkeypatch.setattr(sys, "argv", ["kiro-gateway-tray"])
    monkeypatch.setattr(entry, "_setup_logging", lambda: None)
    monkeypatch.setattr(entry, "_acquire_lock", lambda: False)
    monkeypatch.setattr(entry, "_show_already_running", lambda: None)

    assert entry.main() == 1
    assert events[:2] == ["freeze_support", "parse_args"]
    assert events.count("freeze_support") == 1


def test_freeze_support_is_looked_up_at_call_time(monkeypatch):
    """PyInstaller's runtime hook swaps multiprocessing.freeze_support; the
    entry point must call whatever is installed then, not a stale reference."""
    events = []
    _install_divert_spy(monkeypatch, events)
    entry._divert_multiprocessing_helpers()
    assert events == ["freeze_support"]


def test_tracker_argv_is_diverted_before_argparse_error(monkeypatch, capsys):
    events = []
    _install_divert_spy(monkeypatch, events, exit_on_tracker=True)
    monkeypatch.setattr(sys, "argv", list(_TRACKER_ARGV))

    with pytest.raises(SystemExit) as exc_info:
        entry.main()

    assert exc_info.value.code == 0  # not argparse's exit code 2
    assert events == ["freeze_support"]
    assert "unrecognized arguments" not in capsys.readouterr().err


def test_tracker_argv_without_diversion_is_the_original_bug(monkeypatch, capsys):
    """Documents the failure mode the diversion prevents: argparse rejects the
    resource tracker's argv with exit code 2."""
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(sys, "argv", list(_TRACKER_ARGV))

    with pytest.raises(SystemExit) as exc_info:
        entry.main()

    assert exc_info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_run_gateway_flag_still_dispatches_to_child(monkeypatch):
    events = []
    _install_divert_spy(monkeypatch, events)
    monkeypatch.setattr(sys, "argv", ["kiro-gateway-tray", "--run-gateway"])
    from kiro_gateway_tray import gateway
    monkeypatch.setattr(
        gateway, "run_gateway_blocking", lambda: events.append("gateway") or 0
    )
    monkeypatch.setattr(
        entry, "_setup_logging", lambda: events.append("tray_logging")
    )
    monkeypatch.setattr(entry, "_acquire_lock", lambda: events.append("lock") or True)

    assert entry.main() == 0
    assert events == ["freeze_support", "gateway"]


def test_print_config_flag_unchanged(monkeypatch, tmp_path, capsys):
    events = []
    _install_divert_spy(monkeypatch, events)
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["kiro-gateway-tray", "--print-config"])

    assert entry.main() == 0
    assert capsys.readouterr().out.strip() == str(entry.paths.config_file())
    assert events == ["freeze_support"]


def test_unknown_argument_still_rejected(monkeypatch, capsys):
    events = []
    _install_divert_spy(monkeypatch, events)
    monkeypatch.setattr(sys, "argv", ["kiro-gateway-tray", "--bogus"])

    with pytest.raises(SystemExit) as exc_info:
        entry.main()
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err


def test_module_calls_freeze_support_before_heavy_imports():
    """Static guard: the module-level diversion precedes the loguru-pulling
    imports, so helper processes skip them."""
    source = Path(entry.__file__).read_text(encoding="utf-8")
    divert = source.index("if __name__ == \"__main__\":\n    _divert_multiprocessing_helpers()")
    assert divert < source.index("from .log import logger")
    assert divert < source.index("def main()")
    body = source[source.index("def main()"):]
    assert body.index("_divert_multiprocessing_helpers()") < body.index("argparse.ArgumentParser(")


def test_source_mode_entry_still_runs_end_to_end(tmp_path):
    """Run the real interpreter with ``-m kiro_gateway_tray --print-config``:
    the module-level diversion (stock freeze_support, a no-op off frozen
    builds) must not change normal source-mode behaviour."""
    app_root = Path(entry.__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "kiro_gateway_tray", "--print-config"],
        cwd=str(app_root),
        env={**__import__("os").environ, "KIRO_GATEWAY_TRAY_HOME": str(tmp_path),
             "SENTRY_DSN": "", "PYTHONPATH": str(app_root)},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("config.toml")
