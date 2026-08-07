# app/tests/test_gateway.py
import socket
from pathlib import Path
from kiro_gateway_tray import gateway, appconfig


def test_vendor_root_missing_raises(monkeypatch):
    monkeypatch.setattr(gateway, "_candidate_vendor_roots", lambda: [Path("/no/such")])
    try:
        gateway._vendor_root()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "vendor" in str(e).lower()


def test_apply_env_sets_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = "arn:test"
    cfg.gateway.proxy_api_key = "k123"
    gateway._apply_env(cfg)
    import os
    assert os.environ["PROFILE_ARN"] == "arn:test"
    assert os.environ["PROXY_API_KEY"] == "k123"
    assert os.environ["SERVER_HOST"] == "127.0.0.1"


def test_gateway_env_sets_tiktoken_cache_under_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.AppCfg()

    env = gateway._gateway_env(cfg)

    assert env["TIKTOKEN_CACHE_DIR"] == str(tmp_path / "data" / "tiktoken_cache")


def test_gateway_env_respects_tiktoken_cache_override(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.AppCfg()
    cfg.gateway_extra["TIKTOKEN_CACHE_DIR"] = r"C:\custom\tiktoken"

    env = gateway._gateway_env(cfg)

    assert env["TIKTOKEN_CACHE_DIR"] == r"C:\custom\tiktoken"


def test_child_command_source_mode(monkeypatch):
    monkeypatch.setattr(gateway.sys, "frozen", False, raising=False)
    cmd = gateway._child_command()
    assert cmd[1:] == ["-m", "kiro_gateway_tray", "--run-gateway"]


def test_child_command_frozen_mode(monkeypatch):
    monkeypatch.setattr(gateway.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gateway.sys, "executable", "/Apps/KiroGatewayTray", raising=False)
    cmd = gateway._child_command()
    assert cmd == ["/Apps/KiroGatewayTray", "--run-gateway"]


def test_start_records_gateway_child_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    recorded = []

    class _FakeProc:
        pid = 2468

    monkeypatch.setattr(
        gateway.subprocess, "Popen", lambda *args, **kwargs: _FakeProc()
    )
    monkeypatch.setattr(
        gateway.proc_guard, "record_gateway_pid", recorded.append
    )

    proc = gateway.GatewayProcess()
    proc.start(appconfig.AppCfg())
    proc._close_bootstrap_log()

    assert recorded == [2468]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_wait_port_free_returns_true_when_free():
    # An unbound ephemeral port must be reported free immediately.
    port = _free_port()
    assert gateway.wait_port_free(port, timeout=1) is True


def test_wait_port_free_times_out_while_bound():
    # While a listener holds the port, wait_port_free must give up after timeout.
    # No SO_REUSEADDR here: a real held port mustn't be re-bindable, and on
    # Windows SO_REUSEADDR would let the probe hijack-bind and wrongly pass.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert gateway.wait_port_free(port, timeout=0.5, interval=0.05) is False


def test_wait_port_free_succeeds_after_release():
    # Once the listener closes mid-poll, the next bind probe should succeed.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    srv.close()
    assert gateway.wait_port_free(port, timeout=1, interval=0.05) is True


def test_format_port_busy_message_is_actionable():
    msg = gateway.format_port_busy_message(64005)
    assert "64005" in msg
    assert "占用" in msg
    assert "重新启动" in msg or "重启" in msg


def test_port_busy_error_embeds_message():
    err = gateway.PortBusyError(64005)
    assert err.port == 64005
    assert "64005" in str(err)
    assert "占用" in str(err)


class _FakeProc:
    """Popen stand-in for stop() tests.

    ``exit_after`` controls how many wait() calls happen before the child is
    considered gone: 0 ⇒ exits immediately on the first wait (graceful),
    None ⇒ never exits on its own, so only kill() ends it.
    """

    def __init__(self, *, alive: bool = True, exit_after: int | None = 0) -> None:
        self._alive = alive
        self._exit_after = exit_after
        self._waits = 0
        self.events: list[str] = []

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.events.append("terminate")
        if self._exit_after == 0:
            self._alive = False

    def kill(self):
        self.events.append("kill")
        self._alive = False

    def wait(self, timeout=None):
        self._waits += 1
        self.events.append(f"wait:{timeout}")
        if self._alive:
            raise gateway.subprocess.TimeoutExpired(cmd="gw", timeout=timeout)
        return 0


def _silence_pid_clear(monkeypatch, sink: list[str]) -> None:
    monkeypatch.setattr(
        gateway.proc_guard, "clear_gateway_pid", lambda: sink.append("clear-pid")
    )


def test_graceful_stop_timeout_stays_within_user_patience():
    # The whole point of the constant: a user hitting restart because a request
    # is wedged must not be made to wait out uvicorn's graceful drain. Anything
    # above 5s regresses that, and a non-positive value would skip the graceful
    # path entirely.
    assert 0 < gateway.GRACEFUL_STOP_TIMEOUT <= 5.0
    assert 0 < gateway.KILL_REAP_TIMEOUT <= 5.0


def test_stop_graceful_exit_does_not_kill(monkeypatch):
    # Child honours SIGTERM before the deadline: no SIGKILL escalation, and the
    # graceful wait uses the tuned constant rather than a hardcoded number.
    events: list[str] = []
    _silence_pid_clear(monkeypatch, events)
    proc = _FakeProc(exit_after=0)

    gp = gateway.GatewayProcess()
    gp._proc = proc
    gp.stop()

    assert proc.events == ["terminate", f"wait:{gateway.GRACEFUL_STOP_TIMEOUT}"]
    assert "kill" not in proc.events
    assert events == ["clear-pid"]
    assert gp._proc is None


def test_stop_waits_after_kill(monkeypatch):
    # If terminate() doesn't make the child exit in time, stop() must kill AND
    # wait again so it never returns while the port-holding child is still alive.
    events: list[str] = []
    _silence_pid_clear(monkeypatch, events)
    proc = _FakeProc(exit_after=None)

    gp = gateway.GatewayProcess()
    gp._proc = proc
    gp.stop()

    assert proc.events == [
        "terminate",
        f"wait:{gateway.GRACEFUL_STOP_TIMEOUT}",
        "kill",
        f"wait:{gateway.KILL_REAP_TIMEOUT}",
    ]
    assert events == ["clear-pid"]
    assert gp._proc is None


def test_stop_total_wait_budget_is_bounded_by_constants(monkeypatch):
    # A wedged SSE stream is the worst case: stop() must never block longer than
    # the two configured windows combined, no matter how the child behaves.
    _silence_pid_clear(monkeypatch, [])
    proc = _FakeProc(exit_after=None)

    gp = gateway.GatewayProcess()
    gp._proc = proc
    gp.stop()

    budget = sum(float(e.split(":", 1)[1]) for e in proc.events if e.startswith("wait:"))
    assert budget <= gateway.GRACEFUL_STOP_TIMEOUT + gateway.KILL_REAP_TIMEOUT
    assert budget <= 10.0


def test_stop_survives_child_alive_after_sigkill(monkeypatch):
    # Uninterruptible-state child: the second wait times out too. stop() must
    # still clear bookkeeping and return instead of propagating TimeoutExpired.
    events: list[str] = []
    _silence_pid_clear(monkeypatch, events)

    class _Unkillable(_FakeProc):
        def kill(self):
            self.events.append("kill")  # stays alive on purpose

    proc = _Unkillable(exit_after=None)
    gp = gateway.GatewayProcess()
    gp._proc = proc
    gp.stop()

    assert proc.events == [
        "terminate",
        f"wait:{gateway.GRACEFUL_STOP_TIMEOUT}",
        "kill",
        f"wait:{gateway.KILL_REAP_TIMEOUT}",
    ]
    assert events == ["clear-pid"]
    assert gp._proc is None


def test_stop_already_dead_child_is_not_signalled(monkeypatch):
    # Child crashed or was reaped earlier: signalling a dead pid is pointless and
    # on Windows terminate() on a reaped handle is a needless failure surface.
    events: list[str] = []
    _silence_pid_clear(monkeypatch, events)
    proc = _FakeProc(alive=False)

    gp = gateway.GatewayProcess()
    gp._proc = proc
    gp.stop()

    assert proc.events == []
    assert events == ["clear-pid"]
    assert gp._proc is None


def test_stop_without_child_is_idempotent(monkeypatch):
    # stop() before any start(), or twice in a row, must be a harmless no-op that
    # still clears the recorded pid.
    events: list[str] = []
    _silence_pid_clear(monkeypatch, events)

    gp = gateway.GatewayProcess()
    gp.stop()
    gp.stop()

    assert events == ["clear-pid", "clear-pid"]
    assert gp._proc is None


def test_stop_closes_bootstrap_log_even_when_kill_needed(monkeypatch, tmp_path):
    # The captured-stdout handle must not leak when the child had to be killed.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    _silence_pid_clear(monkeypatch, [])

    gp = gateway.GatewayProcess()
    gp._proc = _FakeProc(exit_after=None)
    log_path = tmp_path / "bootstrap.log"
    handle = open(log_path, "w", encoding="utf-8")
    gp._bootstrap_log = handle

    gp.stop()

    assert handle.closed is True
    assert gp._bootstrap_log is None
