# app/scripts/extract_changelog.py
"""Extract a single version's section from the root ChangeLog.md.

Used by CI to feed the current release's notes into the GitHub Release body.
Given a version (e.g. "0.1.11" or "v0.1.11"), prints the lines between that
version's `## vX.Y.Z ...` heading and the next `## ` heading.

Exit code 0 with the section on stdout when found; exit 0 with empty output
when not found (so CI can fall back to auto-generated notes).

Usage:
    python scripts/extract_changelog.py 0.1.11
    python scripts/extract_changelog.py v0.1.11 --file /path/to/ChangeLog.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parents[2] / "ChangeLog.md"


def extract(text: str, version: str) -> str:
    """Return the changelog body for `version`, or "" if absent.

    The heading line itself is omitted; only the section body is returned,
    trimmed of surrounding blank lines."""
    ver = version.lstrip("v")
    # Match "## vX.Y.Z" optionally followed by a date/suffix, up to the next
    # "## " heading or end of file.
    pattern = re.compile(
        rf"^##\s+v{re.escape(ver)}\b.*?$(?P<body>.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return ""
    return m.group("body").strip("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a version section from ChangeLog.md")
    parser.add_argument("version", help="version to extract, e.g. 0.1.11 or v0.1.11")
    parser.add_argument("--file", default=str(DEFAULT_CHANGELOG), help="path to ChangeLog.md")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"[warn] changelog not found: {path}", file=sys.stderr)
        return 0

    section = extract(path.read_text(encoding="utf-8"), args.version)
    if not section:
        print(f"[warn] no changelog section for {args.version}", file=sys.stderr)
        return 0

    sys.stdout.write(section + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
