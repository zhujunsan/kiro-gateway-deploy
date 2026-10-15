"""Kiro account switches must not leave a stale profile ARN in tray config."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_gateway_tray import appconfig, kiro_identity

_OLD_ARN = "arn:aws:codewhisperer:us-east-1:111111111111:profile/OLD"
_NEW_ARN = "arn:aws:codewhisperer:eu-central-1:222222222222:profile/NEW"


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> appconfig.AppCfg:
    """Create isolated tray configuration for one identity test.

    Args:
        tmp_path: Temporary directory for config and credential files.
        monkeypatch: Fixture isolating the tray home directory.

    Returns:
        Configuration containing a cached ARN for the previous account.
    """
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path / "tray"))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = _OLD_ARN
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


def test_reads_profile_and_prefers_arn_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trim the current ARN and prefer its region over conflicting JSON metadata."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(
        cfg,
        {"profileArn": f"  {_NEW_ARN}  ", "region": "us-west-2"},
    )

    assert kiro_identity.read_current_identity(cfg) == kiro_identity.KiroCredentialIdentity(
        profile_arn=_NEW_ARN, api_region="eu-central-1", enterprise_sso=False
    )


@pytest.mark.parametrize("enterprise_sso", [False, True])
def test_new_profile_replaces_cached_account_and_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enterprise_sso: bool
) -> None:
    """Preserve a valid ARN from the current account regardless of auth type."""
    cfg = _config(tmp_path, monkeypatch)
    identity = kiro_identity.KiroCredentialIdentity(
        profile_arn=_NEW_ARN, api_region="eu-central-1", enterprise_sso=enterprise_sso
    )

    assert kiro_identity.synchronized_values(cfg, identity) == (_NEW_ARN, "eu-central-1")


@pytest.mark.parametrize(
    "sso_fields",
    [
        {"clientIdHash": "new-company-registration"},
        {"clientId": "client-id", "clientSecret": "client-secret"},
    ],
)
def test_enterprise_sso_without_profile_clears_stale_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sso_fields: dict[str, str]
) -> None:
    """Both enterprise credential formats clear an ARN absent from the new file."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"region": "ap-southeast-1", **sso_fields})

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert identity.enterprise_sso is True
    assert identity.profile_arn is None
    assert kiro_identity.synchronized_values(cfg, identity) == ("", "ap-southeast-1")


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
    assert kiro_identity.synchronized_values(cfg, identity) == (_OLD_ARN, "eu-west-1")


@pytest.mark.parametrize("invalid_region", [None, 7, [], "", "not a region", "us--east-1"])
def test_invalid_region_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_region: object
) -> None:
    """Invalid region fields preserve the configured region."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"region": invalid_region})

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert identity.api_region is None
    assert kiro_identity.synchronized_values(cfg, identity)[1] == "us-east-1"


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


@pytest.mark.parametrize(
    "partition,region",
    [("aws", "eu-central-1"), ("aws-cn", "cn-north-1"), ("aws-us-gov", "us-gov-west-1")],
)
def test_profile_region_supports_aws_partitions(partition: str, region: str) -> None:
    """Accept structurally valid profile ARNs across AWS partitions."""
    arn = f"arn:{partition}:codewhisperer:{region}:222222222222:profile/test-id"

    assert kiro_identity.profile_region(arn) == region
