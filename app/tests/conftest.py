# app/tests/conftest.py
"""Shared pytest fixtures for the tray app test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _close_supervisors_after_test(monkeypatch):
    """Stop leftover Supervisor health loops so they cannot leak into later tests.

    ``start()`` launches a background probe thread. Tests that skip ``close()``
    leave that thread running. A later test that stubs ``provision.run`` /
    ``tunnel_exists`` on the module then sees the old loop call those stubs
    (Windows CI: ``test_health_loop_cannot_provision_until_start_finishes``).
    """
    from kiro_gateway_tray.supervisor import Supervisor

    spawned: list = []
    orig_init = Supervisor.__init__

    def _tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        spawned.append(self)

    monkeypatch.setattr(Supervisor, "__init__", _tracking_init)
    yield
    for inst in spawned:
        try:
            inst.close()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _disable_sentry_transport(monkeypatch):
    """Keep unit tests from shipping events to the real Sentry project.

    ``main()`` / gateway startup now call ``init_sentry`` with a baked-in DSN.
    An empty ``SENTRY_DSN`` is the documented kill switch and wins over the
    default, so tests stay offline without mocking every entry point.
    """
    monkeypatch.setenv("SENTRY_DSN", "")
    try:
        from kiro_gateway_tray import sentry_setup as ss
    except ImportError:
        return
    monkeypatch.setattr(ss, "DEFAULT_DSN", "")
    monkeypatch.setattr(ss, "_READY", False)
    monkeypatch.setattr(ss, "_SNAPSHOT_BRIDGE_INSTALLED", False)
