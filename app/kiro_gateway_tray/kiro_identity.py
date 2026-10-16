"""Synchronize gateway account identity from Kiro's credential cache.

The tray only reads local files here and never calls AWS. Enterprise SSO
credential files often lack ``profileArn``; the gateway resolves it itself
after refreshing the token (``ListAvailableProfiles``) and writes it back to
``kiro-auth-token.json``, where the next tray startup picks it up.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import appconfig
from .appconfig import AppCfg

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
        api_region: Region of ``profile_arn``. The file's ``region`` field is
            the SSO (OIDC) region and is deliberately not used: profiles may
            live in a different region (e.g. SSO in ap-northeast-1, profile in
            us-east-1).
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
    candidate_arn = _non_empty_string(data.get("profileArn"))
    arn_region = profile_region(candidate_arn)
    enterprise_sso = bool(
        _non_empty_string(data.get("clientIdHash"))
        or (
            _non_empty_string(data.get("clientId"))
            and _non_empty_string(data.get("clientSecret"))
        )
    )
    return KiroCredentialIdentity(
        profile_arn=candidate_arn if arn_region else None,
        api_region=arn_region,
        enterprise_sso=enterprise_sso,
    )


def synchronized_values(
    cfg: AppCfg, identity: KiroCredentialIdentity
) -> tuple[str, str]:
    """Resolve profile and region values for the next gateway launch.

    - A valid ARN in Kiro's file always wins, together with its region.
    - Without one, the configured ARN is kept as a hint. For enterprise SSO
      the gateway verifies it against ``ListAvailableProfiles`` with a fresh
      token and replaces it when it belongs to another account; an empty hint
      makes the gateway discover the ARN from scratch. Startup never blocks on
      a missing ARN here.
    - The API region follows the chosen ARN; otherwise the configured value
      stays (the gateway corrects it after discovery).

    Args:
        cfg: Current tray configuration.
        identity: Identity parsed from Kiro's current credential file.

    Returns:
        Tuple of ``(profile_arn, api_region)`` to use for startup.
    """
    profile_arn = identity.profile_arn or cfg.gateway.profile_arn
    api_region = (
        identity.api_region
        or profile_region(profile_arn)
        or cfg.gateway.api_region
    )
    return profile_arn, api_region
