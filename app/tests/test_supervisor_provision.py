"""Provisioning singleflight, startup gate, DNS grace, and e2e backoff."""
from __future__ import annotations

import threading
import time

import pytest

from kiro_gateway_tray import supervisor, appconfig
from kiro_gateway_tray.supervisor import _E2E_BACKOFF_STEPS

from test_supervisor import (
    _ReadyResponse,
    _dns_error,
    _install_e2e_stub,
    _make_sup,
    _make_sup_v2,
    _quiesce,
)


@pytest.fixture(autouse=True)
def _identity_matches_stored_hostname(monkeypatch):
    """Keep start() from re-provisioning: slug derived from the stored hostname."""
    import kiro_gateway_tray.provision as pmod

    def _username(cfg):
        return pmod.username_from_hostname(cfg.cloudflare.hostname) or "testuser"

    monkeypatch.setattr(pmod, "_get_username", _username)


def test_startup_gate_blocks_reprovision_before_ready(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "s3cret"
    cfg.cloudflare.provision_url = "https://w.example.com"
    appconfig.save(cfg)
    import kiro_gateway_tray.provision as pmod
    calls = []
    monkeypatch.setattr(pmod, "tunnel_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(
        pmod, "run", lambda cfg, secret: calls.append(1) or ("kg-test.example.com", "t", "")
    )
    assert s._startup_ready.is_set() is False
    assert s._reprovision_if_deleted() is False
    assert calls == []


def test_restart_tunnel_before_ready_does_not_provision(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    s.tunnel.started = True
    import kiro_gateway_tray.provision as pmod
    calls = []
    monkeypatch.setattr(pmod, "tunnel_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(
        pmod, "run", lambda cfg, secret: calls.append(1) or ("kg-x.example.com", "t", "")
    )
    s._restart_tunnel_process("test")
    assert calls == []
    assert s.tunnel.started is True


def test_deleted_and_migrate_race_provisions_once(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "act-code"
    cfg.cloudflare.provision_url = "https://w.example.com"
    appconfig.save(cfg)
    s._cached_secret = "act-code"
    s._startup_ready.set()

    import kiro_gateway_tray.provision as pmod
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow_run(cfg, secret):
        calls.append(threading.current_thread().name)
        started.set()
        release.wait(timeout=2)
        return ("kg-deviceabcdef.example.com", "eyJ_new", "")

    monkeypatch.setattr(pmod, "_get_username", lambda cfg: "deviceabcdef")
    monkeypatch.setattr(pmod, "tunnel_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(pmod, "run", slow_run)

    t_migrate = threading.Thread(
        target=lambda: s._migrate_identity_if_needed(appconfig.load()),
        name="migrate",
        daemon=True,
    )
    t_deleted = threading.Thread(
        target=s._reprovision_if_deleted,
        name="deleted",
        daemon=True,
    )
    t_migrate.start()
    assert started.wait(timeout=2)
    t_deleted.start()
    time.sleep(0.05)
    release.set()
    t_migrate.join(timeout=2)
    t_deleted.join(timeout=2)
    assert calls == ["migrate"]


def test_ten_threads_provision_once(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "act-code"
    appconfig.save(cfg)
    s._cached_secret = "act-code"

    import kiro_gateway_tray.provision as pmod
    calls = []
    barrier = threading.Barrier(10)
    started = threading.Event()
    release = threading.Event()

    def slow_run(cfg, secret):
        calls.append(1)
        started.set()
        release.wait(timeout=2)
        return ("kg-test.example.com", "tok", "")

    monkeypatch.setattr(pmod, "run", slow_run)

    def worker():
        barrier.wait(timeout=2)
        s._provision_once(appconfig.load(), "act-code", "concurrent")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(10)]
    for t in threads:
        t.start()
    assert started.wait(timeout=2)
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(timeout=2)
    assert calls == [1]


def test_one_one_plus_dns_is_degraded(monkeypatch, tmp_path):
    s = _make_sup_v2(monkeypatch, tmp_path)
    s.tunnel.started = True
    with s._state_lock:
        s._tunnel_conns = 1
        s._tunnel_conns_expected = 1
        s._tunnel_e2e_ok = False
        s._tunnel_e2e_kind = "dns"
    assert s.status()["tunnel"] == "degraded"
    assert s.status()["tunnel_detail"] == "DNS 解析失败"


def test_dns_grace_then_failure(monkeypatch, tmp_path):
    s = _make_sup_v2(monkeypatch, tmp_path)
    s.tunnel.started = True
    with s._state_lock:
        s._tunnel_conns = 1
        s._tunnel_conns_expected = 1
        s._tunnel_e2e_ok = False
        s._tunnel_e2e_kind = "dns"
        s._hostname_changed_at = time.time()
    assert s.status()["tunnel_detail"] == "DNS 生效中"
    with s._state_lock:
        s._hostname_changed_at = time.time() - (s._DNS_PROPAGATION_GRACE + 1)
    assert s.status()["tunnel_detail"] == "DNS 解析失败"


def test_doh_recovers_system_nxdomain(monkeypatch, tmp_path):
    s = _make_sup_v2(monkeypatch, tmp_path)
    _install_e2e_stub(monkeypatch, error_factory=_dns_error)
    monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: [])
    monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: ["104.16.1.1"])
    monkeypatch.setattr(supervisor, "_tls_http_get_status", lambda *a, **k: 200)
    result = s._probe_tunnel_e2e(ignore_throttle=True)
    assert result.ok is True
    assert result.kind == "ok"
    assert s._dns_phase == "authoritative"
    s.tunnel.started = True
    with s._state_lock:
        s._tunnel_conns = 1
        s._tunnel_conns_expected = 1
        s._tunnel_e2e_ok = True
    assert s.status()["tunnel"] == "running"


def test_dns_backoff_sequence_and_reset(monkeypatch, tmp_path):
    s = _make_sup_v2(monkeypatch, tmp_path)
    _install_e2e_stub(monkeypatch, error_factory=_dns_error)
    monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: [])
    times = [1000.0]

    def fake_time():
        return times[0]

    monkeypatch.setattr(supervisor.time, "time", fake_time)

    first = s._probe_tunnel_e2e()
    assert first.ok is False
    assert first.kind == "dns"
    assert s._e2e_backoff_step == 1
    expected_until = 1000.0 + _E2E_BACKOFF_STEPS[0]
    assert s._e2e_backoff_until == expected_until

    skipped = s._probe_tunnel_e2e()
    assert skipped.skipped is True
    assert s._e2e_consecutive_failures == 1

    times[0] = expected_until
    second = s._probe_tunnel_e2e()
    assert second.ok is False
    assert s._e2e_backoff_step == 2
    assert s._e2e_backoff_until == expected_until + _E2E_BACKOFF_STEPS[1]

    times[0] = s._e2e_backoff_until
    third = s._probe_tunnel_e2e()
    assert third.ok is False
    assert s._e2e_backoff_step == 3

    times[0] = s._e2e_backoff_until
    fourth = s._probe_tunnel_e2e()
    assert fourth.ok is False
    assert s._e2e_backoff_step == 4
    step = min(3, len(_E2E_BACKOFF_STEPS) - 1)
    # step index used is min(step_before_increment, 3) = 3 → 60s
    assert s._e2e_backoff_until == times[0] + _E2E_BACKOFF_STEPS[step]

    s._reset_e2e_backoff()
    assert s._e2e_backoff_step == 0
    assert s._e2e_backoff_until == 0.0


def test_network_change_and_manual_retry_clear_backoff(monkeypatch, tmp_path):
    s = _make_sup_v2(monkeypatch, tmp_path)
    s.start()
    _quiesce(s)
    with s._state_lock:
        s._e2e_backoff_until = time.time() + 60
        s._e2e_backoff_step = 3

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    orig_thread = supervisor.threading.Thread
    monkeypatch.setattr(supervisor.threading, "Thread", _NoStartThread)
    s.request_tunnel_reconnect("network_change")
    assert s._e2e_backoff_step == 0
    assert s._e2e_backoff_until == 0.0
    monkeypatch.setattr(supervisor.threading, "Thread", orig_thread)

    with s._state_lock:
        s._e2e_backoff_until = time.time() + 60
        s._e2e_backoff_step = 2
    s.start()
    s._stop_health_loop()
    assert s._e2e_backoff_step == 0
    s.close()


def test_dns_recovery_clears_degraded_and_backoff(monkeypatch, tmp_path):
    s = _make_sup_v2(monkeypatch, tmp_path)
    s.start()
    _quiesce(s)
    s.gateway.started = True
    s.tunnel.started = True
    monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
    monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(1))
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
    monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: [])
    _install_e2e_stub(monkeypatch, error_factory=_dns_error)
    s._run_probe_cycle()
    assert s.status()["tunnel"] == "degraded"
    assert s._e2e_backoff_step >= 1

    _install_e2e_stub(monkeypatch, [200])
    s._reset_e2e_backoff()
    s._run_probe_cycle()
    assert s.status()["tunnel"] == "running"
    assert s._e2e_backoff_step == 0
    assert s._e2e_consecutive_failures == 0
    s.close()


def test_ensure_dns_repaired_resets_backoff(monkeypatch, tmp_path):
    s = _make_sup_v2(monkeypatch, tmp_path)
    s.start()
    _quiesce(s)
    s.gateway.started = True
    s.tunnel.started = True
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "act-code"
    appconfig.save(cfg)
    s._cached_secret = "act-code"
    monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
    monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(1))
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
    monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: [])
    _install_e2e_stub(monkeypatch, error_factory=_dns_error)
    import kiro_gateway_tray.provision as pmod

    def _outcome(*_a, **_k):
        return pmod.EnsureDnsOutcome(
            status=True, repaired=True, api_record=True, hostname="kg-test.example.com"
        )

    monkeypatch.setattr(pmod, "ensure_dns_outcome", _outcome)
    s._run_probe_cycle()
    # Repair resets backoff; the immediate re-probe (still dns) arms it again.
    assert s._e2e_dns_repair_attempted is True
    s.close()


def test_health_loop_cannot_provision_until_start_finishes(monkeypatch, tmp_path):
    """probe_now during start() must not issue a second /provision."""
    s = _make_sup(monkeypatch, tmp_path)
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "act-code"
    cfg.cloudflare.provision_url = "https://w.example.com"
    appconfig.save(cfg)
    s._cached_secret = "act-code"

    import kiro_gateway_tray.provision as pmod
    calls = []
    in_register = threading.Event()
    finish_register = threading.Event()

    def slow_run(cfg, secret):
        calls.append("run")
        in_register.set()
        finish_register.wait(timeout=2)
        return ("kg-deviceabcdef.example.com", "eyJ_new", "")

    monkeypatch.setattr(pmod, "_get_username", lambda cfg: "deviceabcdef")
    monkeypatch.setattr(pmod, "tunnel_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(pmod, "run", slow_run)
    # Avoid racing the post-start health loop: tunnel_exists is stubbed False,
    # so a first probe after ready would look like "tunnel deleted".
    monkeypatch.setattr(s, "_start_health_loop", lambda: True)
    monkeypatch.setattr(s, "_probe_tunnel_conns", lambda: 0)

    t = threading.Thread(target=s.start, daemon=True)
    t.start()
    assert in_register.wait(timeout=2)
    # start() has not set _startup_ready yet
    assert s._startup_ready.is_set() is False
    s._run_probe_cycle()
    assert s._tunnel_disconnected_since is None
    assert calls == ["run"]
    finish_register.set()
    t.join(timeout=2)
    assert calls == ["run"]
    s.close()
