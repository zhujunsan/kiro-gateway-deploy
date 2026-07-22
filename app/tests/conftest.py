# app/tests/conftest.py
"""Shared pytest fixtures for the tray app test suite."""
from __future__ import annotations

import pytest


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
