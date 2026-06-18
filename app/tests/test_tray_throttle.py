# app/tests/test_tray_throttle.py
import time

from kiro_gateway_tray.tray import _ThrottleGate


def test_first_entry_always_succeeds():
    gate = _ThrottleGate(min_interval=10.0)
    assert gate.try_enter(now=0.0) is True


def test_reentry_blocked_while_busy():
    gate = _ThrottleGate(min_interval=0.0)
    assert gate.try_enter() is True
    assert gate.try_enter() is False
    gate.done()
    assert gate.try_enter() is True


def test_min_interval_respected():
    gate = _ThrottleGate(min_interval=5.0)
    assert gate.try_enter(now=100.0) is True
    gate.done()
    # Too soon: rejected
    assert gate.try_enter(now=103.0) is False
    # Enough time has passed: admitted
    assert gate.try_enter(now=106.0) is True


def test_zero_interval_only_guards_inflight():
    gate = _ThrottleGate(min_interval=0.0)
    assert gate.try_enter() is True
    gate.done()
    # Immediately re-enterable with interval=0
    assert gate.try_enter() is True
