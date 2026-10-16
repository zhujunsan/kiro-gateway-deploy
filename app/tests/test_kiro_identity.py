"""Kiro account switches must not leave a stale profile ARN in tray config.

The tray reads only local files; profile discovery lives in the gateway. These
tests pin the local rules: a credential ARN wins, otherwise the configured ARN
is passed as a hint, the SSO region never becomes the API region, and nothing
here touches the network or blocks startup.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from kiro_gateway_tray import appconfig, kiro_identity

_OLD_ARN = "arn:aws:codewhisperer:us-east-1:111111111111:profile/OLD"
_NEW_ARN = "arn:aws:codewhisperer:eu-central-1:222222222222:profile/NEW"


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arn: str = _OLD_ARN) -> appconfig.AppCfg:
    """Create isolated tray configuration for one identity test.

    Args:
        tmp_path: Temporary directory for config and credential files.
        monkeypatch: Fixture isolating the tray home directory.
        arn: Cached ARN to store in config ("" simulates a brand-new user).

    Returns:
        Saved configuration pointing at a test credential file.
    """
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path / "tray"))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = arn
    cfg.gateway.api_region = "us-east-1"
    cfg.gateway.kiro_creds_file = str(tmp_path / "kiro-auth-token.json")
    appconfig.save(cfg)
    return cfg


def _write_credentials(cfg: appconfig.AppCfg, payload: object) -> None:
    """Write an isolated Kiro credential fixture.

    Args:
        cfg: Configuration pointing to the test credential file.
        payload: JSON-serializable credential data.
    """
    Path(cfg.gateway.kiro_creds_file).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to open an HTTP client from the identity module."""
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("tray identity sync must not use the network")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)


def test_reads_profile_and_uses_arn_region_not_sso_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trim the current ARN; its region wins over the file's SSO region."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"profileArn": f"  {_NEW_ARN}  ", "region": "us-west-2"})

    assert kiro_identity.read_current_identity(cfg) == kiro_identity.KiroCredentialIdentity(
        profile_arn=_NEW_ARN, api_region="eu-central-1", enterprise_sso=False
    )


@pytest.mark.parametrize("enterprise_sso", [False, True])
def test_credential_arn_replaces_cached_account_and_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enterprise_sso: bool
) -> None:
    """A valid ARN from the current account always wins, regardless of auth type."""
    cfg = _config(tmp_path, monkeypatch)
    identity = kiro_identity.KiroCredentialIdentity(_NEW_ARN, "eu-central-1", enterprise_sso)

    assert kiro_identity.synchronized_values(cfg, identity) == (_NEW_ARN, "eu-central-1")


@pytest.mark.parametrize(
    "sso_fields",
    [
        {"clientIdHash": "company-registration"},
        {"clientId": "client-id", "clientSecret": "client-secret"},
    ],
)
def test_enterprise_without_arn_passes_cached_hint_and_ignores_sso_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sso_fields: dict[str, str]
) -> None:
    """Regression (0.5.x): SSO region ap-northeast-1 must not become the API region,
    and a missing ARN must not block startup; the gateway verifies the hint."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"region": "ap-northeast-1", "accessToken": "expired", **sso_fields})

    identity = kiro_identity.read_current_identity(cfg)

    assert identity == kiro_identity.KiroCredentialIdentity(None, None, True)
    assert kiro_identity.synchronized_values(cfg, identity) == (_OLD_ARN, "us-east-1")


def test_brand_new_enterprise_user_starts_with_empty_arn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config ARN and no credential ARN: launch with empty value for gateway discovery."""
    cfg = _config(tmp_path, monkeypatch, arn="")
    _write_credentials(cfg, {"clientIdHash": "company", "region": "ap-northeast-1"})

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert kiro_identity.synchronized_values(cfg, identity) == ("", "us-east-1")


def test_cached_hint_region_follows_cached_arn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale api_region (e.g. written by 0.5.x from the SSO region) is repaired."""
    cfg = _config(tmp_path, monkeypatch, arn=_NEW_ARN)
    cfg.gateway.api_region = "ap-northeast-1"
    _write_credentials(cfg, {"clientIdHash": "company", "region": "ap-northeast-1"})

    identity = kiro_identity.read_current_identity(cfg)

    assert kiro_identity.synchronized_values(cfg, identity) == (_NEW_ARN, "eu-central-1")


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"clientId": "client-id"},
        {"clientSecret": "client-secret"},
        {"clientIdHash": " "},
        {"clientIdHash": []},
        {"clientId": "id", "clientSecret": None},
    ],
)
def test_non_sso_credentials_without_profile_preserve_manual_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict[str, object]
) -> None:
    """Missing, partial, or malformed SSO markers must not erase a manual ARN."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, fields)

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert identity.enterprise_sso is False
    assert kiro_identity.synchronized_values(cfg, identity) == (_OLD_ARN, "us-east-1")


@pytest.mark.parametrize("invalid_arn", [None, 42, [], "", "not-an-arn",
    "arn:aws:iam::222222222222:role/not-a-profile",
    "arn:aws:codewhisperer:eu-central-1:123:profile/test",
    "arn:aws:codewhisperer:eu-central-1:222222222222:profile/",
    "arn:aws:codewhisperer:eu-central-1:222222222222:profile/a/b",
    "arn:aws:codewhisperer:eu--central-1:222222222222:profile/test",
])
def test_invalid_profile_is_never_copied_into_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_arn: object
) -> None:
    """Reject malformed or incorrectly typed ARN fields without losing fallback."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"profileArn": invalid_arn, "region": "eu-west-1"})

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert identity.profile_arn is None
    assert identity.api_region is None
    assert kiro_identity.synchronized_values(cfg, identity) == (_OLD_ARN, "us-east-1")


@pytest.mark.parametrize("region", [None, 7, [], "", "not a region", "us--east-1", "eu-west-1"])
def test_file_region_never_overrides_configured_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, region: object
) -> None:
    """The file's region is the SSO region; valid or not, it is ignored."""
    cfg = _config(tmp_path, monkeypatch, arn="")
    cfg.gateway.api_region = "eu-central-1"
    _write_credentials(cfg, {"region": region})

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert identity.api_region is None
    assert kiro_identity.synchronized_values(cfg, identity) == ("", "eu-central-1")


@pytest.mark.parametrize("payload", [None, b"{broken", b"[]", b"null", b"42", b"\xff"])
def test_unreadable_or_non_object_credentials_are_not_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes | None
) -> None:
    """Missing, invalid JSON, non-object, and non-UTF8 files are ignored safely."""
    cfg = _config(tmp_path, monkeypatch)
    if payload is not None:
        Path(cfg.gateway.kiro_creds_file).write_bytes(payload)

    assert kiro_identity.read_current_identity(cfg) is None


def test_default_credential_path_is_used_when_custom_path_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read the default Kiro file rather than requiring a custom config path."""
    cfg = _config(tmp_path, monkeypatch)
    credential_path = cfg.gateway.kiro_creds_file
    _write_credentials(cfg, {"profileArn": _NEW_ARN})
    cfg.gateway.kiro_creds_file = ""
    monkeypatch.setattr(appconfig, "default_creds_file", lambda: credential_path)

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert identity.profile_arn == _NEW_ARN


def test_reading_identity_never_modifies_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tray must never rewrite Kiro's login file."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"clientIdHash": "company", "accessToken": "t", "region": "ap-northeast-1"})
    before = Path(cfg.gateway.kiro_creds_file).read_bytes()

    identity = kiro_identity.read_current_identity(cfg)
    kiro_identity.synchronized_values(cfg, identity)

    assert Path(cfg.gateway.kiro_creds_file).read_bytes() == before


@pytest.mark.parametrize(
    "partition,region",
    [("aws", "eu-central-1"), ("aws-cn", "cn-north-1"), ("aws-us-gov", "us-gov-west-1")],
)
def test_profile_region_supports_aws_partitions(partition: str, region: str) -> None:
    """Accept structurally valid profile ARNs across AWS partitions."""
    arn = f"arn:{partition}:codewhisperer:{region}:222222222222:profile/test-id"

    assert kiro_identity.profile_region(arn) == region


def test_tray_no_longer_exposes_network_discovery() -> None:
    """Discovery moved to the gateway; the tray must not reintroduce a blocking path."""
    assert not hasattr(kiro_identity, "discover_profile_arn")
    assert not hasattr(kiro_identity, "resolve_profile_identity")
    assert not hasattr(kiro_identity, "ProfileDiscoveryError")
