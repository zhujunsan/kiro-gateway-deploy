"""Synchronize gateway account identity from Kiro's credential cache."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import appconfig
from .appconfig import AppCfg

_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d+$")
_PROFILE_ARN_RE = re.compile(
    r"^arn:[a-z0-9-]+:codewhisperer:"
    r"(?P<region>[a-z]{2}(?:-[a-z0-9]+)+-\d+):"
    r"\d{12}:profile/[^/\s]+$"
)


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
    credential_path = Path(
        cfg.gateway.kiro_creds_file or appconfig.default_creds_file()
    ).expanduser()
    try:
        data = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

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


def synchronized_values(
    cfg: AppCfg, identity: KiroCredentialIdentity
) -> tuple[str, str]:
    """Resolve profile and region values for the next gateway launch.

    A valid profile ARN from Kiro always supersedes the cached value. AWS SSO
    OIDC credentials may intentionally omit ``profileArn``; in that case an old
    manually cached ARN must be cleared rather than mixed with the new token.
    Desktop credentials without an ARN preserve the configured fallback.

    Args:
        cfg: Current tray configuration.
        identity: Identity parsed from Kiro's current credential file.

    Returns:
        Tuple of ``(profile_arn, api_region)`` to use for startup.
    """
    if identity.profile_arn is not None:
        profile_arn = identity.profile_arn
    elif identity.enterprise_sso:
        profile_arn = ""
    else:
        profile_arn = cfg.gateway.profile_arn

    api_region = identity.api_region or cfg.gateway.api_region
    return profile_arn, api_region
