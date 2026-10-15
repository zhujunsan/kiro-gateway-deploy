#!/usr/bin/env python3
"""Summarize a local audit outbox: raw bytes, deduped blobs, compressed size.

Usage:
    python app/scripts/audit_stats.py [audit_dir]

``audit_dir`` defaults to the tray data directory's ``audit/``. The script
never uploads anything. Numbers are printed as JSON so they can be pasted
back into the design doc.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `python app/scripts/audit_stats.py` from the repo root and from app/.
_APP = Path(__file__).resolve().parents[1]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from kiro_gateway_tray.audit_pipeline import read_zst_lines  # noqa: E402


def summarize(root: Path) -> dict:
    """Add up raw, deduped and compressed sizes under ``root/outbox``.

    Args:
        root: The ``audit/`` directory (the one that contains ``outbox/``).

    Returns:
        A dict of byte counts and request count. Missing outbox is all zeros.
    """
    outbox = root / "outbox"
    raw_request_bytes = 0
    requests = 0
    dropped_media_bytes = 0
    blob_bytes = 0
    compressed_bytes = 0
    if not outbox.exists():
        return {
            "requests": 0,
            "raw_request_bytes": 0,
            "deduped_blob_bytes": 0,
            "compressed_bytes": 0,
            "dropped_media_bytes": 0,
        }
    for path in outbox.rglob("*.jsonl.zst"):
        try:
            compressed_bytes += path.stat().st_size
        except OSError:
            continue
        if path.name.endswith(".req.jsonl.zst"):
            for line in read_zst_lines(path):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                requests += 1
                raw_request_bytes += int(record.get("req_bytes") or 0)
                for drop in record.get("dropped") or []:
                    if isinstance(drop, dict):
                        dropped_media_bytes += int(drop.get("bytes") or 0)
        elif path.name.endswith(".blob.jsonl.zst"):
            for line in read_zst_lines(path):
                blob_bytes += len(line.encode("utf-8")) + 1
    return {
        "requests": requests,
        "raw_request_bytes": raw_request_bytes,
        "deduped_blob_bytes": blob_bytes,
        "compressed_bytes": compressed_bytes,
        "dropped_media_bytes": dropped_media_bytes,
    }


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        root = Path(argv[1])
    else:
        from kiro_gateway_tray import paths
        root = paths.data_dir() / "audit"
    print(json.dumps(summarize(root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
