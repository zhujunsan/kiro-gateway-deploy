# app/tests/test_proc_guard.py
import os
import sys

from kiro_gateway_tray import proc_guard


def test_record_read_clear_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_guard.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(proc_guard.paths, "ensure_dirs", lambda: None)

    assert proc_guard.read_pid() is None
    proc_guard.record_pid(12345)
    assert proc_guard.read_pid() == 12345
    proc_guard.clear_pid()
    assert proc_guard.read_pid() is None


def test_read_pid_ignores_garbage(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_guard.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(proc_guard.paths, "ensure_dirs", lambda: None)
    (tmp_path / "cloudflared.pid").write_text("not-a-number", encoding="utf-8")
    assert proc_guard.read_pid() is None


def test_kill_orphan_no_pid_file(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_guard.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(proc_guard.paths, "ensure_dirs", lambda: None)
    assert proc_guard.kill_orphan() is False


def test_kill_orphan_dead_pid_clears_file(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_guard.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(proc_guard.paths, "ensure_dirs", lambda: None)
    proc_guard.record_pid(424242)
    monkeypatch.setattr(proc_guard, "_pid_is_alive", lambda _pid: False)
    assert proc_guard.kill_orphan() is False
    assert proc_guard.read_pid() is None  # stale file cleaned up


def test_kill_orphan_pid_reuse_guard(monkeypatch, tmp_path):
    # Alive PID that is NOT cloudflared (PID reused by something else) must not
    # be killed.
    monkeypatch.setattr(proc_guard.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(proc_guard.paths, "ensure_dirs", lambda: None)
    proc_guard.record_pid(999)
    monkeypatch.setattr(proc_guard, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(proc_guard, "_looks_like_cloudflared", lambda _pid: False)
    killed = []
    monkeypatch.setattr(proc_guard, "_terminate", lambda pid: killed.append(pid))
    assert proc_guard.kill_orphan() is False
    assert killed == []  # never terminated a non-cloudflared process
    assert proc_guard.read_pid() is None


def test_kill_orphan_terminates_live_cloudflared(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_guard.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(proc_guard.paths, "ensure_dirs", lambda: None)
    proc_guard.record_pid(4321)
    monkeypatch.setattr(proc_guard, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(proc_guard, "_looks_like_cloudflared", lambda _pid: True)
    killed = []
    monkeypatch.setattr(proc_guard, "_terminate", lambda pid: killed.append(pid))
    assert proc_guard.kill_orphan() is True
    assert killed == [4321]
    assert proc_guard.read_pid() is None  # cleared after reaping


def test_pid_is_alive_self():
    # The current process is, definitionally, alive.
    assert proc_guard._pid_is_alive(os.getpid()) is True


def test_spawn_kwargs_per_platform():
    kwargs = proc_guard.spawn_kwargs()
    if sys.platform.startswith("linux"):
        assert "preexec_fn" in kwargs
    elif sys.platform == "win32":
        assert "creationflags" in kwargs
    else:
        assert kwargs == {}


def test_start_records_pid_and_reaps_orphan(monkeypatch):
    # cloudflared.start must reap a prior orphan and record the new PID, so a
    # later session can find and kill a survivor.
    import kiro_gateway_tray.cloudflared as cf
    from kiro_gateway_tray import appconfig

    monkeypatch.setattr(cf, "binary_path", lambda: __import__("pathlib").Path("/fake/cloudflared"))
    monkeypatch.setattr(cf, "_pick_metrics_port", lambda p: p)

    calls = {"reaped": 0, "recorded": None, "after": 0}
    monkeypatch.setattr(cf.proc_guard, "kill_orphan", lambda: calls.__setitem__("reaped", calls["reaped"] + 1) or False)
    monkeypatch.setattr(cf.proc_guard, "spawn_kwargs", lambda: {})
    monkeypatch.setattr(cf.proc_guard, "after_spawn", lambda p: calls.__setitem__("after", calls["after"] + 1))
    monkeypatch.setattr(cf.proc_guard, "record_pid", lambda pid: calls.__setitem__("recorded", pid))

    class _FakeProc:
        pid = 5678
        stdout = None
        def poll(self): return None

    monkeypatch.setattr(cf.subprocess, "Popen", lambda cmd, **kw: _FakeProc())

    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = "eyJ_test"

    cf.CloudflaredProcess().start(cfg)

    assert calls["reaped"] == 1
    assert calls["recorded"] == 5678
    assert calls["after"] == 1


def test_stop_clears_pid(monkeypatch):
    import kiro_gateway_tray.cloudflared as cf
    cleared = {"n": 0}
    monkeypatch.setattr(cf.proc_guard, "clear_pid", lambda: cleared.__setitem__("n", cleared["n"] + 1))
    proc = cf.CloudflaredProcess()
    proc._proc = None  # nothing running
    proc.stop()
    assert cleared["n"] == 1
