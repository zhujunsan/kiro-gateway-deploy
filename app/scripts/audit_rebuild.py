#!/usr/bin/env python3
"""Rebuild client request bodies from a downloaded audit partition tree.

Usage:
    python app/scripts/audit_rebuild.py <audit_or_outbox_dir> [--user SLUG]
        [--kiro-user-id ID] [--start UNIX] [--end UNIX]

Prints one JSON object per request to stdout. A request whose blob is missing
is still printed, with ``_missing_blobs`` listing the hashes, and the same
hashes are repeated on stderr so a gap cannot pass unnoticed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

_APP = Path(__file__).resolve().parents[1]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from kiro_gateway_tray.audit_payload import missing_blob_hashes, rebuild_body  # noqa: E402
from kiro_gateway_tray.audit_pipeline import read_zst_lines  # noqa: E402


def _iter_lines(path: Path) -> Iterator[str]:
    if path.name.endswith(".zst"):
        yield from read_zst_lines(path)
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line:
            yield line


def load_blobs(root: Path) -> dict[str, Any]:
    """Index every blob file under ``root`` by content hash.

    A file that cannot be decompressed or parsed is skipped; the requests that
    needed it surface later as missing hashes.
    """
    found: dict[str, Any] = {}
    for path in root.rglob("*"):
        if not path.is_file() or ".blob.jsonl" not in path.name:
            continue
        try:
            lines = list(_iter_lines(path))
        except Exception as exc:
            print(f"unreadable blob file {path}: {exc}", file=sys.stderr)
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            digest = record.get("h") if isinstance(record, dict) else None
            if isinstance(digest, str) and digest not in found:
                found[digest] = record.get("body")
    return found


def iter_requests(root: Path) -> Iterator[dict]:
    """Yield request records from every req file under ``root``."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".req.jsonl" not in path.name:
            continue
        try:
            lines = list(_iter_lines(path))
        except Exception as exc:
            print(f"unreadable request file {path}: {exc}", file=sys.stderr)
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"malformed request line in {path}", file=sys.stderr)
                continue
            if isinstance(record, dict):
                yield record


def rebuild_record(record: dict, blobs: dict[str, Any]) -> tuple[dict, list[str]]:
    """Attach the reconstructed body. Missing hashes are returned, not dropped.

    Args:
        record: One stored request record.
        blobs: Hash to blob value, usually from :func:`load_blobs`.

    Returns:
        ``(enriched, missing)``. ``enriched["body"]`` is the media-stripped
        request when nothing is missing, otherwise None.
    """
    req = record.get("req") if isinstance(record.get("req"), dict) else {}
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    blob_refs = req.get("blobs") if isinstance(req.get("blobs"), dict) else {}
    missing = missing_blob_hashes(blob_refs, blobs)
    enriched = dict(record)
    if missing:
        enriched["body"] = None
        enriched["_missing_blobs"] = missing
    elif req.get("raw") not in (None, ""):
        enriched["body"] = req.get("raw")
    else:
        enriched["body"] = rebuild_body(params, blob_refs, blobs)
    return enriched, missing


def select(
    root: Path,
    *,
    user: str | None = None,
    kiro_user_id: str | None = None,
    start: float | None = None,
    end: float | None = None,
) -> tuple[list[dict], list[str]]:
    """Rebuild and filter. Returns records plus every missing hash, in order."""
    blobs = load_blobs(root)
    records: list[dict] = []
    missing: list[str] = []
    for record in iter_requests(root):
        if user and record.get("username") != user:
            continue
        if kiro_user_id and record.get("kiro_user_id") != kiro_user_id:
            continue
        ts = record.get("ts_start")
        if start is not None and isinstance(ts, (int, float)) and ts < start:
            continue
        if end is not None and isinstance(ts, (int, float)) and ts >= end:
            continue
        enriched, gaps = rebuild_record(record, blobs)
        records.append(enriched)
        for digest in gaps:
            if digest not in missing:
                missing.append(digest)
    return records, missing


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Rebuild audit requests from an outbox tree")
    parser.add_argument("root", type=Path)
    parser.add_argument("--user", default=None)
    parser.add_argument("--kiro-user-id", default=None)
    parser.add_argument("--start", type=float, default=None)
    parser.add_argument("--end", type=float, default=None)
    args = parser.parse_args(argv[1:])
    records, missing = select(
        args.root,
        user=args.user,
        kiro_user_id=args.kiro_user_id,
        start=args.start,
        end=args.end,
    )
    for record in records:
        print(json.dumps(record, ensure_ascii=False))
    for digest in missing:
        print(f"missing blob {digest}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
