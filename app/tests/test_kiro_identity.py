"""Kiro account switches must not leave a stale profile ARN in tray config."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
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
def test_enterprise_sso_without_profile_requires_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sso_fields: dict[str, str]
) -> None:
    """Both enterprise formats must resolve an ARN before startup can proceed."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"region": "ap-southeast-1", **sso_fields})

    identity = kiro_identity.read_current_identity(cfg)

    assert identity is not None
    assert identity.enterprise_sso is True
    assert identity.profile_arn is None
    with pytest.raises(kiro_identity.ProfileDiscoveryError, match="unresolved"):
        kiro_identity.synchronized_values(cfg, identity)
    assert cfg.gateway.profile_arn == _OLD_ARN


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


@pytest.fixture
def profile_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Install an isolated AWS mock transport and record all discovery requests.

    Args:
        monkeypatch: Fixture replacing the HTTP client.

    Returns:
        Requests captured by the mock transport.
    """
    requests: list[httpx.Request] = []
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        """Return a current-account profile for the default lookup fixture."""
        requests.append(request)
        return httpx.Response(200, json={"profiles": [{"arn": _NEW_ARN}]})

    def client(**kwargs: object) -> httpx.Client:
        """Keep production timeout/proxy options while isolating transport."""
        return real_client(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(kiro_identity.httpx, "Client", client)
    return requests


def test_enterprise_missing_arn_is_discovered_before_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_transport: list[httpx.Request]
) -> None:
    """A new SSO login without profileArn must acquire an ARN instead of erasing it."""
    cfg = _config(tmp_path, monkeypatch)
    credentials = {"clientIdHash": "new-company", "accessToken": "new-account-token", "region": "eu-central-1"}
    _write_credentials(cfg, credentials)
    original_bytes = Path(cfg.gateway.kiro_creds_file).read_bytes()
    identity = kiro_identity.read_current_identity(cfg)
    assert identity is not None

    resolved = kiro_identity.resolve_profile_identity(cfg, identity)

    assert kiro_identity.synchronized_values(cfg, resolved) == (_NEW_ARN, "eu-central-1")
    assert len(profile_transport) == 1
    request = profile_transport[0]
    assert str(request.url) == "https://q.eu-central-1.amazonaws.com/"
    assert request.headers["Authorization"] == "Bearer new-account-token"
    assert request.headers["X-Amz-Target"] == "AmazonCodeWhispererService.ListAvailableProfiles"
    assert json.loads(request.content) == {}
    assert Path(cfg.gateway.kiro_creds_file).read_bytes() == original_bytes
    assert cfg.gateway.profile_arn == _OLD_ARN


@pytest.mark.parametrize("enterprise,arn", [(False, None), (False, _NEW_ARN), (True, _NEW_ARN)])
def test_complete_or_desktop_identity_never_uses_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enterprise: bool, arn: str | None
) -> None:
    """Existing credential ARNs and desktop manual fallbacks stay offline."""
    cfg = _config(tmp_path, monkeypatch)
    identity = kiro_identity.KiroCredentialIdentity(arn, "eu-central-1", enterprise)

    def unexpected(*args: object) -> str:
        """Reject unintended discovery."""
        raise AssertionError("unnecessary network request")

    monkeypatch.setattr(kiro_identity, "discover_profile_arn", unexpected)
    assert kiro_identity.resolve_profile_identity(cfg, identity) is identity


@pytest.mark.parametrize("token", [None, "", " ", [], 123])
def test_missing_access_token_fails_without_launching_empty_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: object
) -> None:
    """Incomplete SSO credentials cannot silently clear a configured ARN."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"clientIdHash": "company", "accessToken": token})
    identity = kiro_identity.read_current_identity(cfg)
    assert identity is not None
    with pytest.raises(kiro_identity.ProfileDiscoveryError, match="Sign in to Kiro"):
        kiro_identity.resolve_profile_identity(cfg, identity)
    assert appconfig.load().gateway.profile_arn == _OLD_ARN


def test_account_change_during_discovery_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not combine an in-flight discovery result with a different login file."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"clientIdHash": "company", "accessToken": "first-token"})
    identity = kiro_identity.read_current_identity(cfg)
    assert identity is not None

    def discover(*args: object) -> str:
        """Simulate a second login replacing credentials during HTTP lookup."""
        _write_credentials(cfg, {"clientIdHash": "company", "accessToken": "second-token"})
        return _NEW_ARN

    monkeypatch.setattr(kiro_identity, "discover_profile_arn", discover)
    with pytest.raises(kiro_identity.ProfileDiscoveryError, match="credentials changed"):
        kiro_identity.resolve_profile_identity(cfg, identity)
    assert appconfig.load().gateway.profile_arn == _OLD_ARN


@pytest.mark.parametrize(
    "profiles,configured,expected",
    [
        ([{"arn": _NEW_ARN}], _OLD_ARN, _NEW_ARN),
        ([None, {"arn": []}, {"arn": "bad"}, {"arn": _NEW_ARN}], "", _NEW_ARN),
        ([{"arn": _NEW_ARN}, {"arn": _NEW_ARN}], "", _NEW_ARN),
        ([{"arn": _NEW_ARN}, {"arn": _NEW_ARN + "2"}], _NEW_ARN, _NEW_ARN),
    ],
)
def test_profile_selection_is_validated_and_preserves_verified_manual_choice(
    monkeypatch: pytest.MonkeyPatch, profiles: list[object], configured: str, expected: str
) -> None:
    """Only profiles returned for this token may be selected, even with old config."""
    real_client = httpx.Client
    monkeypatch.setattr(kiro_identity.httpx, "Client", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"profiles": profiles}))
    ))
    assert kiro_identity.discover_profile_arn("current-token", "eu-central-1", configured) == expected


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"profiles": {}}, {"profiles": []}, {"profiles": [{"arn": _OLD_ARN}]},
     {"profiles": [{"arn": _NEW_ARN}, {"arn": _NEW_ARN + "2"}]},
     {"profiles": [{"arn": _NEW_ARN}], "nextToken": 123}],
)
def test_invalid_empty_wrong_region_and_ambiguous_profiles_fail_closed(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """Neither malformed lists nor multiple new profiles permit a guessed ARN."""
    real_client = httpx.Client
    monkeypatch.setattr(kiro_identity.httpx, "Client", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ))
    with pytest.raises(kiro_identity.ProfileDiscoveryError):
        kiro_identity.discover_profile_arn("current-token", "eu-central-1", _OLD_ARN)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500, 503])
def test_discovery_http_errors_never_expose_response_secrets(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A failed lookup cannot leak server response text or forward bearer redirects."""
    requests: list[httpx.Request] = []
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        """Return a failure containing a secret that must stay out of errors."""
        requests.append(request)
        return httpx.Response(status, text="private-response-token", headers={"Location": "https://example.invalid/"})

    monkeypatch.setattr(kiro_identity.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle)))
    with pytest.raises(kiro_identity.ProfileDiscoveryError) as caught:
        kiro_identity.discover_profile_arn("current-token", "eu-central-1", "")
    assert "private-response-token" not in str(caught.value)
    assert "current-token" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["connect", "timeout", "json"])
def test_transport_and_json_errors_are_actionable_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Transient failures produce an actionable error without raw exception text."""
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        """Simulate errors without making any network calls."""
        if failure == "connect":
            raise httpx.ConnectError("private-token", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-token", request=request)
        return httpx.Response(200, text="not JSON private-token")

    monkeypatch.setattr(kiro_identity.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle)))
    with pytest.raises(kiro_identity.ProfileDiscoveryError, match="Check your network") as caught:
        kiro_identity.discover_profile_arn("current-token", "eu-central-1", "")
    assert "private-token" not in str(caught.value)


def test_all_profile_pages_are_considered_before_selecting(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later page can confirm the previously selected profile belongs to this token."""
    requests: list[httpx.Request] = []
    other = _NEW_ARN + "2"
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        """Return two profile pages."""
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"profiles": [{"arn": other}], "nextToken": "page-2"})
        assert json.loads(request.content) == {"nextToken": "page-2"}
        return httpx.Response(200, json={"profiles": [{"arn": _NEW_ARN}]})

    monkeypatch.setattr(kiro_identity.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle)))
    assert kiro_identity.discover_profile_arn("current-token", "eu-central-1", _NEW_ARN) == _NEW_ARN
    assert len(requests) == 2


@pytest.mark.parametrize("repeat", [True, False])
def test_profile_pagination_is_bounded(monkeypatch: pytest.MonkeyPatch, repeat: bool) -> None:
    """Broken or excessively long pagination cannot hang startup indefinitely."""
    requests: list[httpx.Request] = []
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        """Always return another page, either repeated or unique."""
        requests.append(request)
        return httpx.Response(200, json={"profiles": [{"arn": _NEW_ARN}], "nextToken": "repeat" if repeat else str(len(requests))})

    monkeypatch.setattr(kiro_identity.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle)))
    with pytest.raises(kiro_identity.ProfileDiscoveryError):
        kiro_identity.discover_profile_arn("current-token", "eu-central-1", "")
    assert len(requests) == (2 if repeat else kiro_identity._PROFILE_LOOKUP_MAX_PAGES)


@pytest.mark.parametrize("region", ["", "us--east-1", "us-east-1.evil.invalid", "us-east-1/path", "us-east-1\n"])
def test_invalid_discovery_region_is_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch, region: str
) -> None:
    """Untrusted region values cannot redirect an access token to another host."""
    def unexpected_client(**kwargs: object) -> httpx.Client:
        """Fail if a malformed host reaches the HTTP client."""
        raise AssertionError("Invalid region must fail before network access")

    monkeypatch.setattr(kiro_identity.httpx, "Client", unexpected_client)
    with pytest.raises(kiro_identity.ProfileDiscoveryError, match="region is invalid"):
        kiro_identity.discover_profile_arn("current-token", region, "")


def test_account_metadata_change_before_lookup_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata read before token acquisition must still match that token's file."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"clientIdHash": "company", "accessToken": "first-token", "region": "us-east-1"})
    identity = kiro_identity.read_current_identity(cfg)
    assert identity is not None
    _write_credentials(cfg, {"clientIdHash": "company", "accessToken": "second-token", "region": "eu-central-1"})

    def unexpected(*args: object) -> str:
        """Discovery must not use a stale region for the current token."""
        raise AssertionError("Stale account metadata must be rejected")

    monkeypatch.setattr(kiro_identity, "discover_profile_arn", unexpected)
    with pytest.raises(kiro_identity.ProfileDiscoveryError, match="changed before"):
        kiro_identity.resolve_profile_identity(cfg, identity)
    assert appconfig.load().gateway.profile_arn == _OLD_ARN


def test_credentials_removed_during_lookup_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signing out during a lookup must not persist its in-flight identity."""
    cfg = _config(tmp_path, monkeypatch)
    _write_credentials(cfg, {"clientIdHash": "company", "accessToken": "current-token"})
    identity = kiro_identity.read_current_identity(cfg)
    assert identity is not None

    def discover(*args: object) -> str:
        """Simulate sign-out while the response is in flight."""
        Path(cfg.gateway.kiro_creds_file).unlink()
        return _NEW_ARN

    monkeypatch.setattr(kiro_identity, "discover_profile_arn", discover)
    with pytest.raises(kiro_identity.ProfileDiscoveryError, match="credentials changed"):
        kiro_identity.resolve_profile_identity(cfg, identity)
    assert appconfig.load().gateway.profile_arn == _OLD_ARN
