# app/scripts/inject_metadata.py
"""Rewrite build-time metadata in kiro_gateway_tray/__init__.py.

Reads values from the environment (provided by CI) and patches the
module-level constants in place. Kept as a standalone script so the logic
is testable and not buried in YAML.

Environment variables:
  GITHUB_REPOSITORY      -> GITHUB_REPO  (always applied when set)
  GITHUB_REF_NAME        -> __version__  (only when it looks like "vX.Y.Z")
  UPSTREAM_REPO_OVERRIDE -> UPSTREAM_REPO (optional)
  UPSTREAM_SHA_OVERRIDE  -> UPSTREAM_SHA  (optional)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "kiro_gateway_tray" / "__init__.py"


def _sub_const(text: str, name: str, value: str) -> str:
    """Replace `NAME = "..."` with the given value, preserving formatting."""
    pattern = rf'{name} = "[^"]*"'
    return re.sub(pattern, f'{name} = "{value}"', text, count=1)


def inject(text: str, env: dict[str, str]) -> str:
    repo = env.get("GITHUB_REPOSITORY", "")
    if repo:
        text = _sub_const(text, "GITHUB_REPO", repo)

    tag = env.get("GITHUB_REF_NAME", "")
    if tag.startswith("v"):
        text = _sub_const(text, "__version__", tag.removeprefix("v"))

    upstream_repo = env.get("UPSTREAM_REPO_OVERRIDE", "")
    if upstream_repo:
        text = _sub_const(text, "UPSTREAM_REPO", upstream_repo)

    upstream_sha = env.get("UPSTREAM_SHA_OVERRIDE", "")
    if upstream_sha:
        text = _sub_const(text, "UPSTREAM_SHA", upstream_sha)

    return text


def main() -> None:
    original = TARGET.read_text()
    updated = inject(original, dict(os.environ))
    TARGET.write_text(updated)
    print(f"[ok] injected metadata into {TARGET}")


if __name__ == "__main__":
    main()
