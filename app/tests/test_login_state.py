# app/tests/test_login_state.py
"""Signed-out Kiro credentials must stop polling, not retry forever.

Sentry KIRO-GATEWAY-TRAY-D was 1195 events / 36 users of one signed-out account
being polled every 60s; a single client reached ``consecutive=6883``. These tests
pin the gate that makes that impossible: while signed out we issue one probe per
recheck interval and nothing else, and any success re-arms normal polling.
"""
import pytest

from kiro_gateway_tray import login_state
from kiro_gateway_tray.login_state import LoginGate, LoginState


# --- parse_login_required ----------------------------------------------------

def test_parses_usage_error_shape():
    state = login_state.parse_login_required({
        "error": {
            "code": "usage_auth_required",
            "message": "Kiro credentials are expired",
            "login_required": True,
        }
    })
    assert state.login_required is True
    assert state.code == "usage_auth_required"
    assert "expired" in state.message


def test_parses_health_account_shape():
    state = login_state.parse_login_required({
        "account": {
            "status": "unavailable",
            "code": "account_auth_required",
            "message": "sign in again",
            "login_required": True,
        }
    })
    assert state.login_required is True
    assert state.code == "account_auth_required"


def test_recognises_code_even_without_login_required_flag():
    """Code alone is enough, so an older gateway body still works."""
    state = login_state.parse_login_required({
        "error": {"code": "account_not_configured", "message": "none"}
    })
    assert state.login_required is True
    assert state.not_configured is True


def test_ready_health_is_not_login_required():
    state = login_state.parse_login_required({
        "status": "healthy",
        "account": {"status": "ready", "login_required": False},
    })
    assert state.login_required is False
    assert state.code == ""


def test_unrelated_error_is_not_login_required():
    state = login_state.parse_login_required({
        "error": {"code": "usage_upstream_unreachable", "message": "network"}
    })
    assert state.login_required is False


@pytest.mark.parametrize("payload", [None, "", 0, [], "not json", {"error": "text"}])
def test_malformed_payloads_never_strand_user_in_login_prompt(payload):
    """A broken body must not be read as "you are signed out"."""
    assert login_state.parse_login_required(payload).login_required is False


def test_not_configured_only_for_that_code():
    assert LoginState(login_required=True, code="usage_auth_required").not_configured is False
    assert LoginState(login_required=True, code="account_not_configured").not_configured is True


# --- LoginGate ---------------------------------------------------------------

def _signed_out(code: str = "usage_auth_required") -> LoginState:
    return LoginState(login_required=True, code=code, message="expired")


def test_gate_allows_polling_when_signed_in():
    gate = LoginGate()
    assert gate.login_required is False
    for i in range(10):
        assert gate.should_poll(now=float(i)) is True


def test_gate_blocks_polling_after_signed_out():
    gate = LoginGate(recheck_interval=600.0)
    gate.note_login_required(_signed_out(), now=0.0)

    assert gate.login_required is True
    # Everything inside the window is suppressed — this is the fix for the
    # once-per-minute polling that produced the Sentry flood.
    for t in (1.0, 60.0, 300.0, 599.0):
        assert gate.should_poll(now=t) is False


def test_gate_allows_one_probe_per_interval():
    gate = LoginGate(recheck_interval=100.0)
    gate.note_login_required(_signed_out(), now=0.0)

    assert gate.should_poll(now=100.0) is True     # probe lands
    assert gate.should_poll(now=101.0) is False    # timer re-armed
    assert gate.should_poll(now=200.0) is True     # next window


def test_repeated_failures_do_not_push_the_probe_away():
    """Re-arming on every failure would starve recovery forever."""
    gate = LoginGate(recheck_interval=100.0)
    gate.note_login_required(_signed_out(), now=0.0)
    for t in range(1, 100):
        gate.note_login_required(_signed_out(), now=float(t))

    assert gate.should_poll(now=100.0) is True


def test_signed_in_clears_state_and_resumes_polling():
    gate = LoginGate(recheck_interval=600.0)
    gate.note_login_required(_signed_out(), now=0.0)
    gate.note_signed_in()

    assert gate.login_required is False
    assert gate.state.code == ""
    assert gate.should_poll(now=1.0) is True


def test_force_recheck_bypasses_the_window():
    gate = LoginGate(recheck_interval=600.0)
    gate.note_login_required(_signed_out(), now=0.0)
    assert gate.should_poll(now=10.0) is False

    gate.force_recheck()
    assert gate.should_poll(now=11.0) is True


def test_force_recheck_is_a_noop_when_signed_in():
    gate = LoginGate()
    gate.force_recheck()
    assert gate.login_required is False


def test_note_login_required_ignores_signed_in_state():
    gate = LoginGate()
    gate.note_login_required(LoginState(), now=0.0)
    assert gate.login_required is False


# --- observe_response --------------------------------------------------------

def test_observe_response_latches_signed_out_from_401():
    gate = LoginGate(recheck_interval=100.0)
    state = gate.observe_response(
        status_code=401,
        payload={"error": {"code": "usage_auth_required", "login_required": True}},
        now=0.0,
    )
    assert state.login_required is True
    assert gate.should_poll(now=1.0) is False


def test_observe_response_clears_on_success():
    gate = LoginGate()
    gate.note_login_required(_signed_out(), now=0.0)
    state = gate.observe_response(status_code=200, payload={"breakdowns": []}, now=1.0)
    assert state.login_required is False
    assert gate.login_required is False


def test_observe_response_keeps_signed_out_on_unrelated_failure():
    """A 503 network blip must not be taken as proof we signed back in."""
    gate = LoginGate(recheck_interval=100.0)
    gate.note_login_required(_signed_out(), now=0.0)
    gate.observe_response(
        status_code=503,
        payload={"error": {"code": "usage_upstream_unreachable"}},
        now=1.0,
    )
    assert gate.login_required is True


# --- user-facing text --------------------------------------------------------

def test_menu_hint_is_empty_when_signed_in():
    assert login_state.menu_hint(LoginState()) == ""


def test_menu_hint_distinguishes_expired_from_never_configured():
    expired = login_state.menu_hint(_signed_out("account_auth_required"))
    missing = login_state.menu_hint(_signed_out("account_not_configured"))
    assert "过期" in expired
    assert "未登录" in missing
    assert expired != missing


def test_login_alert_tells_the_user_to_open_kiro():
    title, body = login_state.login_alert_text(_signed_out("account_auth_required"))
    assert "Kiro" in title
    assert "打开 Kiro" in body
    # The gateway's own wording is carried through so surfaces cannot drift.
    assert "expired" in body


def test_login_alert_for_missing_credentials_omits_relogin_wording():
    title, body = login_state.login_alert_text(_signed_out("account_not_configured"))
    assert title == "Kiro 未登录"
    assert "打开 Kiro 并登录" in body


def test_login_alert_without_gateway_message():
    state = LoginState(login_required=True, code="usage_auth_required", message="")
    _title, body = login_state.login_alert_text(state)
    assert "网关说明" not in body


def test_any_login_required():
    assert login_state.any_login_required([LoginState(), _signed_out()]) is True
    assert login_state.any_login_required([LoginState(), LoginState()]) is False
    assert login_state.any_login_required([]) is False


def test_gate_is_thread_safe_under_concurrent_probes():
    """Only one thread per window may get through the gate."""
    import threading

    gate = LoginGate(recheck_interval=100.0)
    gate.note_login_required(_signed_out(), now=0.0)
    allowed: list[bool] = []
    lock = threading.Lock()

    def _probe():
        got = gate.should_poll(now=100.0)
        with lock:
            allowed.append(got)

    threads = [threading.Thread(target=_probe) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert allowed.count(True) == 1
