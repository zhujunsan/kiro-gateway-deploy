# app/kiro_gateway_tray/login_state.py
"""Track whether Kiro credentials are usable, and gate polling on it.

The gateway answers ``401`` with ``login_required: True`` (codes
``usage_auth_required`` / ``account_auth_required`` / ``account_not_configured``)
when the user is signed out of Kiro. Retrying cannot fix that state — only the
user can, by signing in again.

Before this module existed, the tray kept polling ``GET /usage`` every 60s
forever. Because the gateway classified the OIDC ``400 invalid_grant`` as
"upstream unreachable", each poll fed an outage counter and Sentry received an
event every cooldown window (Sentry KIRO-GATEWAY-TRAY-D reached
``consecutive=6883`` for a single user).

Responsibilities kept deliberately narrow:

* remember that we are signed out, so callers can skip work entirely;
* still allow a *low-frequency* probe so signing in is picked up automatically;
* let the user force an immediate re-check from the menu.

The gateway's own credential state is authoritative; this class only caches it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# Gateway error codes that mean "the user must sign in to Kiro again".
LOGIN_REQUIRED_CODES = frozenset({
    "usage_auth_required",
    "account_auth_required",
    "account_not_configured",
})

# Code for "no credentials configured at all" — the wording differs from an
# expired login, because there is nothing to refresh.
NOT_CONFIGURED_CODE = "account_not_configured"

# How long to wait before re-probing while signed out. Long enough that a
# signed-out client is effectively silent, short enough that signing in is
# noticed without touching the menu.
RECHECK_INTERVAL_S = 600.0


@dataclass(frozen=True)
class LoginState:
    """Snapshot of Kiro credential state as last reported by the gateway.

    Attributes:
        login_required: True when the gateway said credentials are unusable.
        code: Gateway error code, or empty when signed in.
        message: Actionable text from the gateway, or empty when signed in.
    """

    login_required: bool = False
    code: str = ""
    message: str = ""

    @property
    def not_configured(self) -> bool:
        """Whether the failure is "never signed in" rather than "expired"."""
        return self.code == NOT_CONFIGURED_CODE


def _coerce_mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` when it is a mapping, else an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def parse_login_required(payload: Any) -> LoginState:
    """Extract credential state from a gateway JSON body.

    Handles both shapes the gateway emits:

    * ``/usage`` errors: ``{"error": {"code": ..., "login_required": true}}``
    * ``/health``: ``{"account": {"code": ..., "login_required": true}}``

    Matching is by ``login_required`` / ``code``, never by message text, so
    wording changes upstream cannot silently disable the tray's handling.

    Args:
        payload: Parsed JSON body, or any value (non-mappings yield a signed-in
            state so malformed responses never strand the user in a login prompt).

    Returns:
        LoginState describing whether re-login is required.
    """
    body = _coerce_mapping(payload)
    for key in ("error", "account"):
        section = _coerce_mapping(body.get(key))
        if not section:
            continue
        code = str(section.get("code") or "")
        flagged = bool(section.get("login_required")) or code in LOGIN_REQUIRED_CODES
        if flagged:
            return LoginState(
                login_required=True,
                code=code,
                message=str(section.get("message") or ""),
            )
    return LoginState()


class LoginGate:
    """Thread-safe gate that suppresses polling while Kiro is signed out.

    Callers ask :meth:`should_poll` before doing work. While signed out it
    returns False except for one probe every :data:`RECHECK_INTERVAL_S`, so a
    user who signs in is picked up without any manual action.

    All methods are safe to call from the tray thread and worker threads.
    """

    def __init__(self, *, recheck_interval: float = RECHECK_INTERVAL_S) -> None:
        """
        Args:
            recheck_interval: Seconds between probes while signed out.
        """
        self._recheck_interval = recheck_interval
        self._lock = threading.Lock()
        self._state = LoginState()
        self._next_probe_at = 0.0

    @property
    def state(self) -> LoginState:
        """Return the last known credential state."""
        with self._lock:
            return self._state

    @property
    def login_required(self) -> bool:
        """Whether the user currently needs to sign in to Kiro."""
        with self._lock:
            return self._state.login_required

    def note_login_required(self, state: LoginState, *, now: float | None = None) -> None:
        """Record that the gateway reported unusable credentials.

        Args:
            state: Parsed state; ignored when ``login_required`` is False.
            now: Injectable monotonic clock for tests.
        """
        if not state.login_required:
            return
        ts = time.monotonic() if now is None else now
        with self._lock:
            self._state = state
            # Only arm the timer on the transition, so repeated failures inside a
            # window cannot push the next probe further and further away.
            if self._next_probe_at == 0.0:
                self._next_probe_at = ts + self._recheck_interval

    def note_signed_in(self) -> None:
        """Clear the signed-out state after any successful authenticated call."""
        with self._lock:
            self._state = LoginState()
            self._next_probe_at = 0.0

    def force_recheck(self) -> None:
        """Allow the next :meth:`should_poll` through immediately.

        Backs the menu's manual "re-check" action: a user who just signed in
        should not have to wait out the interval.
        """
        with self._lock:
            if self._state.login_required:
                self._next_probe_at = 0.0

    def should_poll(self, *, now: float | None = None) -> bool:
        """Return whether a caller may issue an authenticated request now.

        While signed in this is always True. While signed out it is True only
        once per recheck interval; that probe re-arms the timer, so a still
        signed-out user produces one request per interval instead of one per
        minute.

        Args:
            now: Injectable monotonic clock for tests.

        Returns:
            True when the caller should proceed.
        """
        ts = time.monotonic() if now is None else now
        with self._lock:
            if not self._state.login_required:
                return True
            if ts < self._next_probe_at:
                return False
            self._next_probe_at = ts + self._recheck_interval
            return True

    def observe_response(
        self,
        *,
        status_code: int,
        payload: Any = None,
        now: float | None = None,
    ) -> LoginState:
        """Update state from an authenticated gateway response.

        Args:
            status_code: HTTP status the gateway returned.
            payload: Parsed JSON body, when available.
            now: Injectable monotonic clock for tests.

        Returns:
            The credential state after applying this response.
        """
        state = parse_login_required(payload)
        if state.login_required:
            self.note_login_required(state, now=now)
            return state
        if 200 <= status_code < 300:
            # Any successful authenticated call proves credentials work again.
            self.note_signed_in()
        return self.state


def menu_hint(state: LoginState) -> str:
    """Return the Chinese menu text for a signed-out state.

    Args:
        state: Current credential state.

    Returns:
        Text for the quota row; empty string when signed in.
    """
    if not state.login_required:
        return ""
    if state.not_configured:
        return "未登录 Kiro（点此了解）"
    return "Kiro 登录已过期（点此重新检测）"


def login_alert_text(state: LoginState) -> tuple[str, str]:
    """Return (title, body) for the re-login dialog / notification.

    The body prefers the gateway's own message so the two surfaces cannot drift,
    and always states the concrete action the user must take.

    Args:
        state: Current credential state.

    Returns:
        Tuple of dialog title and body text.
    """
    if state.not_configured:
        title = "Kiro 未登录"
        action = "请打开 Kiro 并登录，然后回到本应用点击「重新检测」。"
    else:
        title = "Kiro 登录已过期"
        action = "请打开 Kiro 重新登录（或执行 kiro-cli login），然后点击「重新检测」。"
    detail = state.message.strip()
    body = f"{action}\n\n网关说明：{detail}" if detail else action
    return title, body


def any_login_required(states: Iterable[LoginState]) -> bool:
    """Return whether any of ``states`` requires re-login."""
    return any(state.login_required for state in states)
