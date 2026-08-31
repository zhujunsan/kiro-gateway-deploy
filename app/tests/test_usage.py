# app/tests/test_usage.py
import httpx
import pytest

from kiro_gateway_tray import usage
from kiro_gateway_tray.login_state import LoginState


class _FakeResponse:
    """Minimal httpx.Response stand-in for the localhost gateway probes."""

    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _patch_client(monkeypatch, response=None, *, error: Exception | None = None):
    """Route usage module HTTP calls to a stub, recording requested paths."""
    calls: list[str] = []

    def _get(url, **_kwargs):
        calls.append(url)
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(usage._client, "get", _get)
    return calls


class TestAuthedGetLoginRequired:
    """A signed-out gateway must raise LoginRequiredError, not a generic error.

    Callers branch on the type to stop polling; a bare RuntimeError would keep
    the once-per-minute retry loop that flooded Sentry (KIRO-GATEWAY-TRAY-D).
    """

    def test_401_with_login_required_raises_login_required_error(self, monkeypatch):
        _patch_client(monkeypatch, _FakeResponse(401, {
            "error": {
                "code": "usage_auth_required",
                "message": "Kiro credentials are expired",
                "login_required": True,
            }
        }))

        with pytest.raises(usage.LoginRequiredError) as excinfo:
            usage.fetch()

        assert excinfo.value.state.code == "usage_auth_required"
        assert excinfo.value.state.login_required is True

    def test_account_not_configured_also_raises_login_required(self, monkeypatch):
        _patch_client(monkeypatch, _FakeResponse(401, {
            "error": {"code": "account_not_configured", "message": "none"}
        }))

        with pytest.raises(usage.LoginRequiredError) as excinfo:
            usage.fetch()

        assert excinfo.value.state.not_configured is True

    def test_503_upstream_unreachable_stays_a_plain_runtime_error(self, monkeypatch):
        """Real outages must keep retrying, so they must not be LoginRequiredError."""
        _patch_client(monkeypatch, _FakeResponse(
            503,
            {"error": {"code": "usage_upstream_unreachable"}},
            text="unreachable",
        ))

        with pytest.raises(RuntimeError) as excinfo:
            usage.fetch()

        assert not isinstance(excinfo.value, usage.LoginRequiredError)

    def test_non_json_error_body_does_not_become_login_required(self, monkeypatch):
        _patch_client(monkeypatch, _FakeResponse(500, None, text="<html>oops"))

        with pytest.raises(RuntimeError) as excinfo:
            usage.fetch()

        assert not isinstance(excinfo.value, usage.LoginRequiredError)
        assert "500" in str(excinfo.value)

    def test_success_returns_payload(self, monkeypatch):
        _patch_client(monkeypatch, _FakeResponse(200, {"breakdowns": []}))
        assert usage.fetch() == {"breakdowns": []}


class TestFetchHealth:
    """/health is the cheap, upstream-free way to learn credential state."""

    def test_reports_login_required_from_account_block(self, monkeypatch):
        calls = _patch_client(monkeypatch, _FakeResponse(200, {
            "status": "degraded",
            "account": {
                "code": "account_auth_required",
                "message": "sign in again",
                "login_required": True,
            },
        }))

        state = usage.fetch_health()

        assert state.login_required is True
        assert state.code == "account_auth_required"
        assert calls and calls[0].endswith("/health")

    def test_ready_account_is_signed_in(self, monkeypatch):
        _patch_client(monkeypatch, _FakeResponse(200, {
            "status": "healthy",
            "account": {"status": "ready", "login_required": False},
        }))
        assert usage.fetch_health() == LoginState()

    def test_old_gateway_without_account_block_is_signed_in(self, monkeypatch):
        """Must stay compatible with gateways that predate the account field."""
        _patch_client(monkeypatch, _FakeResponse(200, {"status": "healthy"}))
        assert usage.fetch_health().login_required is False

    def test_unreachable_gateway_never_claims_signed_out(self, monkeypatch):
        _patch_client(monkeypatch, None, error=httpx.ConnectError("refused"))
        assert usage.fetch_health().login_required is False

    def test_non_json_health_body_never_claims_signed_out(self, monkeypatch):
        _patch_client(monkeypatch, _FakeResponse(200, None, text="nope"))
        assert usage.fetch_health().login_required is False


def test_format_summary_with_overage():
    data = {
        "subscription": "Kiro Pro",
        "breakdowns": [
            {"used": 1100, "limit": 1000, "overage": 100, "overageCostUsd": 4.0},
        ],
        "overageRateUsd": 0.04,
        "overageCreditsTotal": 100,
        "overageCostUsd": 4.0,
    }
    out = usage.format_summary(data)
    assert "Kiro Pro" in out
    assert "1100 / 1000" in out
    assert "超额 100" in out
    assert "$4.0" in out
    assert "预计超额费用: $4.0" in out


def test_format_summary_no_overage():
    data = {
        "subscription": "Kiro Pro",
        "breakdowns": [{"used": 500, "limit": 1000, "overage": 0, "overageCostUsd": 0.0}],
        "overageCostUsd": 0.0,
    }
    out = usage.format_summary(data)
    assert "500 / 1000" in out
    assert "超额" not in out
    assert "预计超额费用" not in out


def test_format_menu_line_with_overage():
    data = {
        "breakdowns": [{"used": 1100, "limit": 1000}],
        "overageCostUsd": 4.0,
    }
    assert usage.format_menu_line(data) == "1100 / 1000 ($4.0)"


def test_format_menu_line_no_overage():
    data = {"breakdowns": [{"used": 500, "limit": 1000}], "overageCostUsd": 0.0}
    assert usage.format_menu_line(data) == "500 / 1000"


def test_format_menu_line_empty():
    assert usage.format_menu_line({"breakdowns": []}) == "无数据"


def test_split_models_for_menu_pairs_aliases_and_keeps_native_models():
    ids = sorted([
        "auto",
        "claude-haiku-4.5",
        "claude-opus-4.6",
        "claude-sonnet-5",
        "deepseek-3.2",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "kiro-deepseek-3.2",
        "kiro-h-4.5",
        "kiro-o-4.6",
        "kiro-s-5",
    ])
    aliases = {
        "kiro-h-4.5": "claude-haiku-4.5",
        "kiro-o-4.6": "claude-opus-4.6",
        "kiro-s-5": "claude-sonnet-5",
        "kiro-deepseek-3.2": "deepseek-3.2",
    }
    canonical, alias_list = usage.split_models_for_menu(ids, aliases=aliases)
    assert alias_list == [
        "kiro-h-4.5",
        "kiro-o-4.6",
        "kiro-s-5",
        "kiro-deepseek-3.2",
    ]
    assert canonical == [
        "auto",
        "claude-haiku-4.5",
        "claude-opus-4.6",
        "claude-sonnet-5",
        "deepseek-3.2",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]


def test_split_models_for_menu_unpaired_real_appended():
    ids = ["claude-haiku-4.5", "mystery-model", "kiro-h-4.5"]
    aliases = {"kiro-h-4.5": "claude-haiku-4.5"}
    canonical, alias_list = usage.split_models_for_menu(ids, aliases=aliases)
    assert canonical == ["claude-haiku-4.5", "mystery-model"]
    assert alias_list == ["kiro-h-4.5"]


def test_split_models_for_menu_pairs_opus_5_via_generate_alias(monkeypatch):
    """Newly discovered models pair via generate_model_alias without static table."""
    import sys
    import types

    fake_aliases = types.ModuleType("kiro.model_aliases")

    def generate_model_alias(model_id: str):
        mapping = {
            "claude-opus-5": "kiro-o-5",
            "claude-haiku-4.5": "kiro-h-4.5",
        }
        return mapping.get(model_id)

    fake_aliases.generate_model_alias = generate_model_alias  # type: ignore[attr-defined]
    fake_kiro = types.ModuleType("kiro")
    fake_kiro.model_aliases = fake_aliases  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro", fake_kiro)
    monkeypatch.setitem(sys.modules, "kiro.model_aliases", fake_aliases)

    ids = [
        "claude-haiku-4.5",
        "claude-opus-5",
        "kiro-h-4.5",
        "kiro-o-5",
    ]
    canonical, alias_list = usage.split_models_for_menu(ids, aliases={})
    assert canonical == ["claude-haiku-4.5", "claude-opus-5"]
    assert alias_list == ["kiro-h-4.5", "kiro-o-5"]
