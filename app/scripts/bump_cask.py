# app/scripts/bump_cask.py
"""Update the Homebrew cask in this repo to a released version.

The cask lives at the repo root in `Casks/kiro-gateway-tray.rb` and ships two
architectures (arm64 / amd64), each with its own `sha256` inside an
`on_arm` / `on_intel` block. At release time CI knows the version and both
DMG digests, and rewrites the cask in place so `brew upgrade` picks it up.

Kept as a standalone, import-safe function so the rewrite logic is unit
tested instead of buried in YAML.

Usage:
    python scripts/bump_cask.py 0.1.14 \
        --arm-sha <arm64-dmg-sha256> \
        --intel-sha <amd64-dmg-sha256>
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CASK = Path(__file__).resolve().parents[2] / "Casks" / "kiro-gateway-tray.rb"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def bump(text: str, version: str, arm_sha: str, intel_sha: str) -> str:
    """Return the cask text with version and both sha256 values replaced.

    Raises ValueError if the expected anchors are missing so a malformed
    cask fails the release loudly instead of shipping stale hashes.
    """
    version = version.lstrip("v")
    for name, sha in (("arm", arm_sha), ("intel", intel_sha)):
        if not _SHA_RE.match(sha):
            raise ValueError(f"{name} sha256 is not a 64-char hex digest: {sha!r}")

    new, n = re.subn(r'^(\s*version\s+)"[^"]*"',
                     rf'\g<1>"{version}"', text, count=1, flags=re.MULTILINE)
    if n != 1:
        raise ValueError("could not find a single `version \"...\"` line in cask")
    text = new

    # The arm sha256 is the one inside the `on_arm do ... end` block, the
    # intel sha256 inside `on_intel do ... end`. Replace each scoped to its
    # block so order in the file doesn't matter.
    def _replace_block(src: str, block: str, sha: str) -> str:
        pattern = re.compile(
            rf"(on_{block} do.*?sha256\s+)\"[0-9a-fA-F]*\"",
            re.DOTALL,
        )
        out, count = pattern.subn(rf'\g<1>"{sha}"', src, count=1)
        if count != 1:
            raise ValueError(f"could not find sha256 inside on_{block} block")
        return out

    text = _replace_block(text, "arm", arm_sha)
    text = _replace_block(text, "intel", intel_sha)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump the Homebrew cask to a release")
    parser.add_argument("version", help="release version, e.g. 0.1.14 or v0.1.14")
    parser.add_argument("--arm-sha", required=True, help="sha256 of the macos-arm64 DMG")
    parser.add_argument("--intel-sha", required=True, help="sha256 of the macos-amd64 DMG")
    parser.add_argument("--file", default=str(DEFAULT_CASK), help="path to the cask .rb")
    args = parser.parse_args()

    path = Path(args.file)
    updated = bump(path.read_text(encoding="utf-8"), args.version, args.arm_sha, args.intel_sha)
    path.write_text(updated, encoding="utf-8")
    print(f"[ok] bumped cask to v{args.version.lstrip('v')}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
