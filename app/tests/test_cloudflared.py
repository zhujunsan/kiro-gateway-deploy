# app/tests/test_cloudflared.py
from pathlib import Path
from kiro_gateway_tray import cloudflared, appconfig


def test_binary_name_per_platform():
    import sys
    name = cloudflared._binary_name()
    if sys.platform.startswith("win"):
        assert name == "cloudflared.exe"
    else:
        assert name == "cloudflared"


def test_binary_path_missing_raises(monkeypatch):
    monkeypatch.setattr(cloudflared, "_candidate_dirs", lambda: [Path("/no/such")])
    try:
        cloudflared.binary_path()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "cloudflared" in str(e).lower()


def test_start_pins_metrics_port(monkeypatch, tmp_path):
    # cloudflared must be launched with a fixed --metrics address so the
    # /ready probe has a stable target; the configured port is used when free.
    import kiro_gateway_tray.cloudflared as cf

    monkeypatch.setattr(cf, "binary_path", lambda: Path("/fake/cloudflared"))
    monkeypatch.setattr(cf, "_port_is_free", lambda _p: True)
    monkeypatch.setattr(cf.proc_guard, "kill_orphan", lambda: False)
    monkeypatch.setattr(cf.proc_guard, "after_spawn", lambda _p: None)
    monkeypatch.setattr(cf.proc_guard, "record_pid", lambda _pid: None)

    captured = {}

    class _FakeProc:
        pid = 4242
        stdout = None
        def poll(self): return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(cf.subprocess, "Popen", fake_popen)

    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.metrics_port = 20299
    cfg.cloudflare.protocol = "http2"

    proc = cf.CloudflaredProcess()
    proc.start(cfg)

    cmd = captured["cmd"]
    assert "--metrics" in cmd
    assert cmd[cmd.index("--metrics") + 1] == "127.0.0.1:20299"
    assert proc.metrics_port == 20299
    # Windows system code page (e.g. GBK) must not decode UTF-8 cloudflared logs.
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"


def test_start_falls_back_when_metrics_port_busy(monkeypatch):
    # A busy metrics port must NOT be fatal: cloudflared treats a failed metrics
    # bind as fatal and exits, so we fall back to a free port and the probe
    # follows the port we actually bound.
    import kiro_gateway_tray.cloudflared as cf

    monkeypatch.setattr(cf, "binary_path", lambda: Path("/fake/cloudflared"))
    # Configured port is busy; the OS-assigned fallback is free.
    monkeypatch.setattr(cf, "_port_is_free", lambda p: p != 20299)
    monkeypatch.setattr(cf.proc_guard, "kill_orphan", lambda: False)
    monkeypatch.setattr(cf.proc_guard, "after_spawn", lambda _p: None)
    monkeypatch.setattr(cf.proc_guard, "record_pid", lambda _pid: None)

    captured = {}

    class _FakeProc:
        pid = 4242
        stdout = None
        def poll(self): return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(cf.subprocess, "Popen", fake_popen)

    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.metrics_port = 20299

    proc = cf.CloudflaredProcess()
    proc.start(cfg)

    cmd = captured["cmd"]
    assert "--metrics" in cmd
    bound = cmd[cmd.index("--metrics") + 1]
    assert bound != "127.0.0.1:20299"
    assert bound.startswith("127.0.0.1:")
    # The probe target must track the actually-bound port, not the config value.
    assert proc.metrics_port != 20299
    assert proc.metrics_port == int(bound.rsplit(":", 1)[1])


def test_start_requires_run_token():
    import kiro_gateway_tray.cloudflared as cf
    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = ""
    try:
        cf.CloudflaredProcess().start(cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "run_token" in str(e)


def test_watch_output_swallows_reader_errors(monkeypatch):
    # A dying reader thread used to leave stdout unread → pipe backpressure.
    # Any iteration/decode failure must be logged and exit cleanly.
    import kiro_gateway_tray.cloudflared as cf

    warnings = []

    class _BoomStdout:
        def __iter__(self):
            raise UnicodeDecodeError("gbk", b"\xff", 0, 1, "boom")

    class _FakeProc:
        stdout = _BoomStdout()

    monkeypatch.setattr(cf, "_build_log_writer", lambda: (lambda _line: None))
    monkeypatch.setattr(cf.logger, "warning", lambda *a, **k: warnings.append((a, k)))

    proc = cf.CloudflaredProcess()
    proc._proc = _FakeProc()
    proc._watch_output()  # must not raise

    assert warnings, "expected warning when stdout reader fails"


class _FakeStdin:
    """Records every write/flush so command framing can be asserted."""

    def __init__(self, write_exc=None, flush_exc=None, hook=None):
        self.events = []
        self.closed = False
        self._write_exc = write_exc
        self._flush_exc = flush_exc
        self._hook = hook

    def write(self, data):
        if self._hook:
            self._hook()
        if self._write_exc:
            raise self._write_exc
        self.events.append(("write", data))
        return len(data)

    def flush(self):
        if self._flush_exc:
            raise self._flush_exc
        self.events.append(("flush", None))

    def close(self):
        self.closed = True

    def writes(self):
        return [d for kind, d in self.events if kind == "write"]


class _ControlProc:
    """Minimal Popen stand-in with a writable stdin and controllable liveness."""

    pid = 4242
    stdout = None

    def __init__(self, stdin=None, exit_code=None):
        self.stdin = stdin if stdin is not None else _FakeStdin()
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self._exit_code or 0


def _proc_with_stdin(stdin=None, exit_code=None):
    """Build a CloudflaredProcess already 'started' with a fake child."""
    import kiro_gateway_tray.cloudflared as cf

    proc = cf.CloudflaredProcess()
    proc._proc = _ControlProc(stdin=stdin, exit_code=exit_code)
    return proc


def test_start_enables_stdin_control(monkeypatch):
    # The stdin control channel must be requested at spawn time: it is the only
    # way to trigger a backoff-free edge reconnect without killing the process.
    import kiro_gateway_tray.cloudflared as cf

    monkeypatch.setattr(cf, "binary_path", lambda: Path("/fake/cloudflared"))
    monkeypatch.setattr(cf, "_port_is_free", lambda _p: True)
    monkeypatch.setattr(cf.proc_guard, "kill_orphan", lambda: False)
    monkeypatch.setattr(cf.proc_guard, "after_spawn", lambda _p: None)
    monkeypatch.setattr(cf.proc_guard, "record_pid", lambda _pid: None)

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _ControlProc()

    monkeypatch.setattr(cf.subprocess, "Popen", fake_popen)

    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = "eyJ_test"
    cf.CloudflaredProcess().start(cfg)

    cmd = captured["cmd"]
    assert "--stdin-control" in cmd
    # It is a global flag: cloudflared rejects it after the `run` subcommand.
    assert cmd.index("--stdin-control") < cmd.index("run")
    assert cmd.index("--stdin-control") > cmd.index("tunnel")
    assert captured["kwargs"]["stdin"] is cf.subprocess.PIPE


def test_request_reconnect_writes_one_command_per_connection():
    # The reconnect signal is unicast (one command wakes exactly one HA
    # connection), so N connections require exactly N commands.
    stdin = _FakeStdin()
    proc = _proc_with_stdin(stdin)

    assert proc.request_reconnect(3) is True
    assert stdin.writes() == ["reconnect\n"] * 3
    # A single flush after the batch is enough; commands must not be buffered.
    assert stdin.events[-1] == ("flush", None)


def test_request_reconnect_clamps_count():
    import kiro_gateway_tray.cloudflared as cf

    for given, expected in [
        (0, 1),
        (1, 1),
        (-5, 1),
        (4, 4),
        (9, cf.HA_CONNECTIONS_MAX),
        (10_000, cf.HA_CONNECTIONS_MAX),
    ]:
        stdin = _FakeStdin()
        proc = _proc_with_stdin(stdin)
        assert proc.request_reconnect(given) is True
        assert len(stdin.writes()) == expected, f"count={given}"
    assert cf.HA_CONNECTIONS_MAX == 4


def test_request_reconnect_accepts_float_like_count():
    # Callers derive the count from observed metrics; a non-int numeric must not
    # blow up the write loop.
    stdin = _FakeStdin()
    proc = _proc_with_stdin(stdin)
    assert proc.request_reconnect(2.9) is True
    assert len(stdin.writes()) == 2


def test_request_reconnect_false_when_never_started():
    import kiro_gateway_tray.cloudflared as cf

    assert cf.CloudflaredProcess().request_reconnect(4) is False


def test_request_reconnect_false_when_process_exited():
    # A dead child must degrade to False so the caller escalates to a restart
    # instead of believing the tunnel was healed.
    stdin = _FakeStdin()
    proc = _proc_with_stdin(stdin, exit_code=1)

    assert proc.request_reconnect(4) is False
    assert stdin.writes() == []


def test_request_reconnect_false_when_stdin_missing():
    proc = _proc_with_stdin()
    proc._proc.stdin = None
    assert proc.request_reconnect(4) is False


def test_request_reconnect_false_on_broken_pipe():
    # cloudflared can die between poll() and write(); the race must not raise.
    stdin = _FakeStdin(write_exc=BrokenPipeError("gone"))
    proc = _proc_with_stdin(stdin)
    assert proc.request_reconnect(4) is False


def test_request_reconnect_false_on_os_error():
    stdin = _FakeStdin(write_exc=OSError("pipe busted"))
    proc = _proc_with_stdin(stdin)
    assert proc.request_reconnect(2) is False


def test_request_reconnect_false_on_closed_stdin_value_error():
    # Writing to a file object closed by stop() raises ValueError, not OSError.
    stdin = _FakeStdin(write_exc=ValueError("I/O operation on closed file"))
    proc = _proc_with_stdin(stdin)
    assert proc.request_reconnect(1) is False


def test_request_reconnect_false_when_flush_fails():
    # Buffered-but-unflushed commands never reach cloudflared, so a failing
    # flush must be reported as failure too.
    stdin = _FakeStdin(flush_exc=BrokenPipeError("gone"))
    proc = _proc_with_stdin(stdin)
    assert proc.request_reconnect(2) is False


def test_request_reconnect_does_not_interleave_across_threads():
    # Two concurrent callers (network watcher + supervisor) must not splice
    # their commands together; each batch stays contiguous and ends in a flush.
    import threading

    barrier = threading.Barrier(2)
    first = {"done": False}

    def hook():
        # Force overlap: the first writer parks inside the critical section.
        if not first["done"]:
            first["done"] = True
            barrier.wait(timeout=5)

    stdin = _FakeStdin(hook=hook)
    proc = _proc_with_stdin(stdin)
    results = []

    def call():
        results.append(proc.request_reconnect(3))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    barrier.wait(timeout=5)
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert results == [True, True]
    kinds = [kind for kind, _ in stdin.events]
    assert kinds == ["write"] * 3 + ["flush"] + ["write"] * 3 + ["flush"]


def test_stop_closes_stdin():
    stdin = _FakeStdin()
    proc = _proc_with_stdin(stdin)
    child = proc._proc

    proc.stop()

    assert stdin.closed is True
    assert child.terminated is True
    # After stop the control channel is dead; further requests must fail loudly
    # rather than silently no-op.
    proc._proc._exit_code = 0
    assert proc.request_reconnect(4) is False


def test_stop_survives_stdin_close_error():
    class _BadStdin(_FakeStdin):
        def close(self):
            raise OSError("already gone")

    proc = _proc_with_stdin(_BadStdin())
    child = proc._proc
    proc.stop()  # must not raise
    assert child.terminated is True


def test_stop_without_start_is_noop():
    import kiro_gateway_tray.cloudflared as cf

    cf.CloudflaredProcess().stop()  # must not raise


def test_provision_username_from_client_id_hash(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    # No profileArn anywhere -> fall back to clientIdHash.
    monkeypatch.setattr(provision, "_read_kiro_token", lambda _cfg: None)
    monkeypatch.setattr(
        provision, "_read_client_id_hash", lambda _data: "ABCDEF0123456789abcdef"
    )
    # first 12 hex chars, lowercased
    assert provision._get_username(cfg) == "abcdef012345"


def test_provision_username_missing_hash_raises(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    monkeypatch.setattr(provision, "_read_kiro_token", lambda _cfg: None)
    monkeypatch.setattr(provision, "_read_client_id_hash", lambda _data: None)
    try:
        provision._get_username(cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "clientIdHash" in str(e)


def test_provision_username_prefers_per_user_client_id(monkeypatch):
    """Per-user clientId (unique per user) takes precedence over org-shared clientIdHash."""
    import hashlib
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    monkeypatch.setattr(provision, "_read_per_user_client_id", lambda _cfg, _data: "my-unique-client-id")
    monkeypatch.setattr(provision, "_read_client_id_hash", lambda _data: "ABCDEF0123456789abcdef")
    expected = hashlib.sha1("my-unique-client-id".encode()).hexdigest()[:12]
    assert provision._get_username(cfg) == expected


def test_provision_config_profile_arn_overrides_token(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    cfg.gateway.profile_arn = "arn:aws:codewhisperer:eu-west-1:999:profile/CFG"
    monkeypatch.setattr(
        provision, "_read_kiro_token",
        lambda _cfg: {"profileArn": "arn:aws:codewhisperer:us-east-1:111:profile/TOK"},
    )
    assert provision.read_profile_arn(cfg) == cfg.gateway.profile_arn
    assert provision.read_api_region(cfg) == "eu-west-1"


def test_region_from_arn():
    from kiro_gateway_tray import provision
    arn = "arn:aws:codewhisperer:ap-northeast-1:123456789012:profile/ABC"
    assert provision.region_from_arn(arn) == "ap-northeast-1"
    assert provision.region_from_arn("") == ""
    assert provision.region_from_arn("not-an-arn") == ""


def test_provision_read_profile_arn_and_region(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    arn = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABC"
    monkeypatch.setattr(provision, "_read_kiro_token", lambda _cfg: {"profileArn": arn})
    assert provision.read_profile_arn(cfg) == arn
    assert provision.read_api_region(cfg) == "us-east-1"


def test_post_with_retry_retries_on_5xx(monkeypatch):
    from kiro_gateway_tray import provision

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "x"

    calls = {"n": 0}

    def fake_post(url, json, timeout, **kwargs):
        calls["n"] += 1
        return _Resp(500 if calls["n"] < 3 else 200)

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    monkeypatch.setattr(provision.time, "sleep", lambda _s: None)
    resp = provision._post_with_retry("http://x/provision", {})
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_post_with_retry_no_retry_on_401(monkeypatch):
    from kiro_gateway_tray import provision

    class _Resp:
        status_code = 401
        text = "nope"

    calls = {"n": 0}

    def fake_post(url, json, timeout, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    monkeypatch.setattr(provision.time, "sleep", lambda _s: None)
    resp = provision._post_with_retry("http://x/provision", {})
    assert resp.status_code == 401
    assert calls["n"] == 1  # client error returned immediately, not retried
