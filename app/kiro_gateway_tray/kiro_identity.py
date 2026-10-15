"""Synchronize gateway account identity from Kiro's credential cache."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from . import appconfig
from .httpclient import resolve_proxy
from .appconfig import AppCfg

_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d+$")
_PROFILE_ARN_RE = re.compile(
    r"^arn:[a-z0-9-]+:codewhisperer:"
    r"(?P<region>[a-z]{2}(?:-[a-z0-9]+)+-\d+):"
    r"\d{12}:profile/[^/\s]+$"
)


_PROFILE_LOOKUP_TIMEOUT = 10.0
_PROFILE_LOOKUP_MAX_PAGES = 10


class ProfileDiscoveryError(RuntimeError):
    """Current credentials could not be mapped to an unambiguous profile."""


@dataclass(frozen=True)
class KiroCredentialIdentity:
    """Account identity discovered in Kiro's current credential file.

    Attributes:
        profile_arn: Valid CodeWhisperer profile ARN, when the file provides one.
        api_region: Region associated with the current credentials, when valid.
        enterprise_sso: Whether the file represents AWS SSO OIDC credentials.
    """

    profile_arn: str | None
    api_region: str | None
    enterprise_sso: bool


def _non_empty_string(value: Any) -> str | None:
    """Return a stripped non-empty string, or None for other values.

    Args:
        value: Untrusted value read from the credential JSON.

    Returns:
        A stripped string when non-empty; otherwise None.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def profile_region(profile_arn: str | None) -> str | None:
    """Extract a validated region from a CodeWhisperer profile ARN.

    Args:
        profile_arn: Candidate profile ARN.

    Returns:
        Region from a valid ARN, or None when malformed or absent.
    """
    if not profile_arn:
        return None
    match = _PROFILE_ARN_RE.fullmatch(profile_arn)
    return match.group("region") if match else None


def _credential_path(cfg: AppCfg) -> Path:
    """Return the selected credential path with user-home expansion.

    Args:
        cfg: Current tray configuration.

    Returns:
        Kiro credential file path.
    """
    return Path(
        cfg.gateway.kiro_creds_file or appconfig.default_creds_file()
    ).expanduser()


def _read_credentials(cfg: AppCfg) -> dict[str, Any] | None:
    """Read credentials without logging or modifying their contents.

    Args:
        cfg: Configuration selecting the credential file.

    Returns:
        JSON object, or None when absent, unreadable, or malformed.
    """
    try:
        data = json.loads(_credential_path(cfg).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_current_identity(cfg: AppCfg) -> KiroCredentialIdentity | None:
    """Read the account identity currently selected by Kiro.

    The well-known ``kiro-auth-token.json`` file is replaced when the user logs
    into another personal or company account. An unreadable or malformed file
    returns None so startup preserves the last known-good gateway configuration.

    Args:
        cfg: Current tray configuration, including an optional credential path.

    Returns:
        Parsed credential identity, or None when the credential file cannot be
        read as a JSON object.
    """
    data = _read_credentials(cfg)
    if data is None:
        return None

    return _identity_from_credentials(data)


def _identity_from_credentials(data: dict[str, Any]) -> KiroCredentialIdentity:
    """Parse account metadata from one credential snapshot.

    Args:
        data: Untrusted JSON object from Kiro's credential file.

    Returns:
        Validated account metadata without tokens or client secrets.
    """
    candidate_arn = _non_empty_string(data.get("profileArn"))
    arn_region = profile_region(candidate_arn)
    profile_arn = candidate_arn if arn_region else None

    candidate_region = _non_empty_string(data.get("region"))
    file_region = (
        candidate_region
        if candidate_region and _REGION_RE.fullmatch(candidate_region)
        else None
    )
    enterprise_sso = bool(
        _non_empty_string(data.get("clientIdHash"))
        or (
            _non_empty_string(data.get("clientId"))
            and _non_empty_string(data.get("clientSecret"))
        )
    )
    return KiroCredentialIdentity(
        profile_arn=profile_arn,
        api_region=arn_region or file_region,
        enterprise_sso=enterprise_sso,
    )


def discover_profile_arn(
    access_token: str, api_region: str, configured_arn: str
) -> str:
    """Discover a profile belonging to the current bearer token.

    Enterprise SSO token files often omit profileArn even though the chat API
    requires it. ListAvailableProfiles verifies membership without using the
    previous account's ARN, refreshing tokens, or modifying login files.

    Args:
        access_token: Current access token, used only in the authorization header.
        api_region: Validated region of the management endpoint.
        configured_arn: Previously selected ARN; reusable only if returned by AWS.

    Returns:
        The sole available ARN, or the configured ARN when AWS confirms it.

    Raises:
        ProfileDiscoveryError: Lookup fails or no unambiguous profile exists.
    """
    if not _REGION_RE.fullmatch(api_region):
        raise ProfileDiscoveryError("Kiro API region is invalid. Check the gateway configuration.")
    candidates: set[str] = set()
    next_token: str | None = None
    seen_tokens: set[str] = set()
    try:
        with httpx.Client(
            timeout=_PROFILE_LOOKUP_TIMEOUT,
            proxy=resolve_proxy(),
            follow_redirects=False,
        ) as client:
            for _ in range(_PROFILE_LOOKUP_MAX_PAGES):
                response = client.post(
                    f"https://q.{api_region}.amazonaws.com/",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/x-amz-json-1.0",
                        "X-Amz-Target": "AmazonCodeWhispererService.ListAvailableProfiles",
                    },
                    json={"nextToken": next_token} if next_token else {},
                )
                if response.status_code != 200:
                    raise ProfileDiscoveryError(
                        "Kiro profile discovery failed "
                        f"(HTTP {response.status_code}). Sign in to Kiro again and restart the gateway."
                    )
                result = response.json()
                if not isinstance(result, dict) or not isinstance(result.get("profiles"), list):
                    raise ProfileDiscoveryError(
                        "Kiro returned an invalid profile list. Retry restarting the gateway."
                    )
                for profile in result["profiles"]:
                    if isinstance(profile, dict):
                        arn = _non_empty_string(profile.get("arn"))
                        if arn and profile_region(arn) == api_region:
                            candidates.add(arn)
                raw_next = result.get("nextToken")
                if raw_next is not None and not isinstance(raw_next, str):
                    raise ProfileDiscoveryError("Kiro returned an invalid profile page token.")
                next_token = _non_empty_string(raw_next)
                if not next_token:
                    break
                if next_token in seen_tokens:
                    raise ProfileDiscoveryError("Kiro repeated a profile page. Retry restarting the gateway.")
                seen_tokens.add(next_token)
            else:
                raise ProfileDiscoveryError("Kiro returned too many profile pages. Select a profile manually.")
    except (httpx.HTTPError, ValueError):
        # Never include response bodies, request headers, or exception text:
        # they can contain credentials supplied by the remote peer.
        raise ProfileDiscoveryError(
            "Kiro profile discovery is unavailable. Check your network and Kiro login, then restart the gateway."
        ) from None

    if configured_arn in candidates:
        return configured_arn
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        raise ProfileDiscoveryError(
            "No Kiro profile is available in the current API region. Check your account and region, then restart the gateway."
        )
    raise ProfileDiscoveryError(
        "This Kiro account has multiple profiles. Set the intended profileArn in the gateway configuration, then restart."
    )


def resolve_profile_identity(
    cfg: AppCfg, identity: KiroCredentialIdentity
) -> KiroCredentialIdentity:
    """Fill an enterprise identity's missing ARN using its current access token.

    Args:
        cfg: Current configuration and previously selected profile.
        identity: Locally parsed credential identity.

    Returns:
        A complete identity, or the original one when discovery is unnecessary.

    Raises:
        ProfileDiscoveryError: No access token exists, discovery fails, or the
            credential file changes during lookup. Callers must not launch with
            an empty or unverified profile in these cases.
    """
    if identity.profile_arn or not identity.enterprise_sso:
        return identity
    credentials = _read_credentials(cfg)
    token = _non_empty_string(credentials.get("accessToken")) if credentials else None
    if not token:
        raise ProfileDiscoveryError(
            "Kiro profileArn is missing and no access token is available. Sign in to Kiro again, then restart the gateway."
        )
    if credentials is not None and _identity_from_credentials(credentials) != identity:
        raise ProfileDiscoveryError(
            "Kiro credentials changed before profile discovery. Restart the gateway to use the current account."
        )
    region = identity.api_region or cfg.gateway.api_region
    arn = discover_profile_arn(token, region, cfg.gateway.profile_arn)
    if _read_credentials(cfg) != credentials:
        raise ProfileDiscoveryError(
            "Kiro credentials changed during profile discovery. Restart the gateway to use the current account."
        )
    return replace(identity, profile_arn=arn, api_region=profile_region(arn))


def synchronized_values(
    cfg: AppCfg, identity: KiroCredentialIdentity
) -> tuple[str, str]:
    """Resolve profile and region values for the next gateway launch.

    A valid profile ARN from Kiro always supersedes the cached value. AWS SSO
    OIDC credentials may omit ``profileArn``; callers must resolve it for the
    current token before synchronization. An unresolved enterprise identity
    blocks startup instead of launching with an empty or stale profile.
    Desktop credentials without an ARN preserve the configured fallback.

    Args:
        cfg: Current tray configuration.
        identity: Identity parsed from Kiro's current credential file.

    Returns:
        Tuple of ``(profile_arn, api_region)`` to use for startup.

    Raises:
        ProfileDiscoveryError: An enterprise profile has not been resolved.
    """
    if identity.profile_arn is not None:
        profile_arn = identity.profile_arn
    elif identity.enterprise_sso:
        raise ProfileDiscoveryError(
            "Kiro profileArn is unresolved. Sign in to Kiro and restart the gateway to discover the current profile."
        )
    else:
        profile_arn = cfg.gateway.profile_arn

    api_region = identity.api_region or cfg.gateway.api_region
    return profile_arn, api_region
