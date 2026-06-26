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


def test_child_command_source_mode(monkeypatch):
    monkeypatch.setattr(gateway.sys, "frozen", False, raising=False)
    cmd = gateway._child_command()
    assert cmd[1:] == ["-m", "kiro_gateway_tray", "--run-gateway"]


def test_child_command_frozen_mode(monkeypatch):
    monkeypatch.setattr(gateway.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gateway.sys, "executable", "/Apps/KiroGatewayTray", raising=False)
    cmd = gateway._child_command()
    assert cmd == ["/Apps/KiroGatewayTray", "--run-gateway"]


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


def test_stop_waits_after_kill(monkeypatch):
    # If terminate() doesn't make the child exit in time, stop() must kill AND
    # wait again so it never returns while the port-holding child is still alive.
    events = []

    class _FakeProc:
        def __init__(self):
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")
            self._alive = False

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            if self._alive and "kill" not in events:
                raise gateway.subprocess.TimeoutExpired(cmd="gw", timeout=timeout)
            return 0

    gp = gateway.GatewayProcess()
    gp._proc = _FakeProc()
    gp.stop()

    assert events == ["terminate", "wait:10", "kill", "wait:5"]
    assert gp._proc is None
