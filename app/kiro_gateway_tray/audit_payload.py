"""Pure functions for audit request splitting.

Media (images, documents, other base64 payloads) is replaced with a placeholder.
The remaining JSON is content-addressed: each top-level array element, and any
other top-level field whose canonical JSON exceeds ``BLOB_MIN_CHARS``, becomes a
blob keyed by sha256. ``rebuild_body`` puts a request back together from those
blobs. Nothing here touches disk or the network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = 1

# Canonical JSON longer than this (strictly) is stored as its own blob.
BLOB_MIN_CHARS = 2048
# data: URLs shorter than or equal to this stay inline.
DATA_URL_MIN_CHARS = 4096

REDACTED = "<redacted>"
REDACT_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "proxy-authorization",
    "cookie",
    "cf-access-client-secret",
})

_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,", re.IGNORECASE)


def canonical_json(obj: Any) -> str:
    """Stable JSON used both for hashing and for the size threshold.

    Key order does not affect the result. Non-ASCII is kept as-is so the hash
    of Chinese or emoji content does not depend on an escape setting.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj: Any) -> str:
    """sha256 hex digest of :func:`canonical_json`."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _b64_bytes(data: str) -> bytes:
    """Decode a raw or ``data:`` base64 payload. Falls back to the UTF-8 bytes."""
    payload = data
    if payload.startswith("data:"):
        comma = payload.find(",")
        payload = payload[comma + 1:] if comma >= 0 else ""
    try:
        return base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return data.encode("utf-8", errors="replace")


def _media_type_of_data_url(value: str) -> str | None:
    match = _DATA_URL_RE.match(value)
    return match.group(1) if match else None


def _placeholder(kind: str, raw: str, media_type: str | None, path: str) -> tuple[dict, dict]:
    decoded = _b64_bytes(raw)
    digest = hashlib.sha256(decoded).hexdigest()
    placeholder = {"_dropped": kind, "bytes": len(decoded), "sha256": digest}
    dropped = {
        "path": path,
        "kind": kind,
        "media_type": media_type,
        "bytes": len(decoded),
        "sha256": digest,
    }
    return placeholder, dropped


def _match_block(node: dict, path: str) -> tuple[dict, list[dict]] | None:
    """Return a replacement when ``node`` is a known base64 media block."""
    kind_type = node.get("type")
    source = node.get("source")
    if kind_type in ("image", "document") and isinstance(source, dict) and source.get("type") == "base64":
        data = source.get("data")
        if isinstance(data, str):
            media_type = source.get("media_type")
            if not isinstance(media_type, str):
                media_type = None
            placeholder, dropped = _placeholder(kind_type, data, media_type, path)
            return placeholder, [dropped]

    if kind_type == "image_url":
        image_url = node.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.startswith("data:"):
            placeholder, dropped = _placeholder("image", url, _media_type_of_data_url(url), path)
            return placeholder, [dropped]

    if kind_type == "input_image":
        url = node.get("image_url")
        if isinstance(url, str) and url.startswith("data:"):
            placeholder, dropped = _placeholder("image", url, _media_type_of_data_url(url), path)
            return placeholder, [dropped]

    if kind_type == "input_file":
        file_data = node.get("file_data")
        if isinstance(file_data, str) and file_data:
            media_type = node.get("media_type") or node.get("mime_type")
            if not isinstance(media_type, str):
                media_type = _media_type_of_data_url(file_data)
            placeholder, dropped = _placeholder("file", file_data, media_type, path)
            return placeholder, [dropped]
    return None


def _match_data_url(value: str, path: str) -> tuple[dict, list[dict]] | None:
    """Fallback for a long ``data:<mime>;base64,`` string that is not a known block."""
    if len(value) <= DATA_URL_MIN_CHARS:
        return None
    media_type = _media_type_of_data_url(value)
    if media_type is None:
        return None
    kind = "image" if media_type.startswith("image/") else "file"
    placeholder, dropped = _placeholder(kind, value, media_type, path)
    return placeholder, [dropped]


def _walk(node: Any, path: str) -> tuple[Any, list[dict]]:
    if isinstance(node, dict):
        matched = _match_block(node, path)
        if matched is not None:
            return matched
        rebuilt: dict[str, Any] = {}
        dropped: list[dict] = []
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            new_value, child_dropped = _walk(value, child)
            rebuilt[key] = new_value
            dropped.extend(child_dropped)
        return rebuilt, dropped
    if isinstance(node, list):
        rebuilt_list: list[Any] = []
        dropped = []
        for index, value in enumerate(node):
            new_value, child_dropped = _walk(value, f"{path}[{index}]")
            rebuilt_list.append(new_value)
            dropped.extend(child_dropped)
        return rebuilt_list, dropped
    if isinstance(node, str):
        matched = _match_data_url(node, path)
        if matched is not None:
            return matched
    return node, []


def strip_media(body: dict) -> tuple[dict, list[dict]]:
    """Return a copy of ``body`` with base64 media replaced, plus drop records.

    The input is not modified. Each drop record has ``path``, ``kind``,
    ``media_type``, ``bytes`` (decoded length) and ``sha256`` of the decoded
    bytes. The replacement object is ``{"_dropped", "bytes", "sha256"}``.

    Args:
        body: Parsed JSON object (a request body).

    Returns:
        ``(stripped, dropped)``. ``stripped`` is a new dict.

    Raises:
        TypeError: ``body`` is not a dict.
    """
    if not isinstance(body, dict):
        raise TypeError(f"strip_media expects a dict, got {type(body).__name__}")
    stripped, dropped = _walk(body, "")
    return stripped, dropped


def split_body(body: dict) -> tuple[dict, dict[str, Any], dict[str, Any]]:
    """Split a (already media-stripped) request object into params and blobs.

    Top-level arrays are split per element. Any other top-level value whose
    canonical JSON is longer than ``BLOB_MIN_CHARS`` becomes one blob. The rest
    stays in ``params`` unchanged.

    Args:
        body: JSON object. Not modified.

    Returns:
        ``(params, blobs, new_blobs)``. ``blobs`` maps a field name to a hash
        or a list of hashes. ``new_blobs`` maps each hash to its value; duplicate
        hashes inside one body appear once.
    """
    if not isinstance(body, dict):
        raise TypeError(f"split_body expects a dict, got {type(body).__name__}")
    params: dict[str, Any] = {}
    blobs: dict[str, Any] = {}
    new_blobs: dict[str, Any] = {}
    for key, value in body.items():
        if isinstance(value, list):
            hashes: list[str] = []
            for item in value:
                digest = canonical_hash(item)
                hashes.append(digest)
                new_blobs.setdefault(digest, item)
            blobs[key] = hashes
        else:
            encoded = canonical_json(value)
            if len(encoded) > BLOB_MIN_CHARS:
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                blobs[key] = digest
                new_blobs.setdefault(digest, value)
            else:
                params[key] = value
    return params, blobs, new_blobs


def split_request_bytes(raw: bytes) -> tuple[dict, list[dict], dict[str, Any]]:
    """Turn raw request bytes into the stored ``req`` object.

    A body that is not a JSON object (empty, invalid JSON, array, string,
    non-UTF-8) is kept as ``req["raw"]`` decoded with ``errors="replace"``,
    and ``params`` / ``blobs`` stay empty. No exception is raised.

    Args:
        raw: Exact request body bytes.

    Returns:
        ``(req, dropped, new_blobs)`` where ``req`` is
        ``{"params", "blobs", "raw"}``.
    """
    text = raw.decode("utf-8", errors="replace") if raw else ""
    empty = {"params": {}, "blobs": {}, "raw": text}
    if not raw:
        return empty, [], {}
    try:
        # Decode first. json.loads(bytes) sniffs a UTF-16 BOM and can raise
        # UnicodeDecodeError, which must not escape this function.
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return empty, [], {}
    if not isinstance(parsed, dict):
        return {"params": {}, "blobs": {}, "raw": text}, [], {}
    stripped, dropped = strip_media(parsed)
    params, blobs, new_blobs = split_body(stripped)
    return {"params": params, "blobs": blobs, "raw": None}, dropped, new_blobs


def rebuild_body(
    params: dict,
    blobs: dict[str, Any],
    blob_lookup: dict[str, Any],
) -> dict:
    """Rebuild a media-stripped request body from params and blob hashes.

    Args:
        params: Fields that were stored inline.
        blobs: Field name to a hash or a list of hashes.
        blob_lookup: Hash to the original value.

    Returns:
        The reconstructed object. Key order follows ``params`` then ``blobs``.

    Raises:
        KeyError: A referenced hash is absent from ``blob_lookup``. The missing
            hash is the exception argument.
    """
    rebuilt = dict(params)
    for key, ref in blobs.items():
        if isinstance(ref, list):
            values = []
            for digest in ref:
                if digest not in blob_lookup:
                    raise KeyError(digest)
                values.append(blob_lookup[digest])
            rebuilt[key] = values
        else:
            if ref not in blob_lookup:
                raise KeyError(ref)
            rebuilt[key] = blob_lookup[ref]
    return rebuilt


def missing_blob_hashes(blobs: dict[str, Any], blob_lookup: dict[str, Any]) -> list[str]:
    """Return referenced hashes that ``blob_lookup`` does not contain, in order."""
    missing: list[str] = []
    for ref in blobs.values():
        refs = ref if isinstance(ref, list) else [ref]
        for digest in refs:
            if digest not in blob_lookup and digest not in missing:
                missing.append(digest)
    return missing


def redact_headers(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """Lower-case header names and mask credential headers.

    Repeated headers are joined with ``", "`` (HTTP list semantics). Comparison
    against :data:`REDACT_HEADERS` is case-insensitive. Values are decoded as
    latin-1, which cannot fail for arbitrary bytes.

    Args:
        headers: ASGI header pairs.

    Returns:
        A plain dict safe to persist.
    """
    merged: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        if name in REDACT_HEADERS:
            value = REDACTED
        else:
            value = raw_value.decode("latin-1")
        if name in merged:
            merged[name] = merged[name] + ", " + value
        else:
            merged[name] = value
    return merged
