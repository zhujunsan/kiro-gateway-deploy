"""Audit payload splitting: media stripping, content-addressed blobs, rebuild."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from kiro_gateway_tray.audit_payload import (
    BLOB_MIN_CHARS,
    DATA_URL_MIN_CHARS,
    REDACTED,
    canonical_hash,
    canonical_json,
    missing_blob_hashes,
    rebuild_body,
    redact_headers,
    split_body,
    split_request_bytes,
    strip_media,
)


def _png_b64(size: int = 64) -> str:
    return base64.b64encode(b"\x89PNG" + b"x" * size).decode()


def _data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _assert_rebuilt(original: dict) -> None:
    stripped, _dropped = strip_media(original)
    params, blobs, new_blobs = split_body(stripped)
    rebuilt = rebuild_body(params, blobs, new_blobs)
    assert canonical_json(rebuilt) == canonical_json(stripped)


# --- media stripping ---------------------------------------------------------

def test_anthropic_image_and_document_are_replaced():
    image_data = _png_b64()
    doc_data = base64.b64encode(b"%PDF-1.4 rest").decode()
    body = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": doc_data}},
            ],
        }],
    }
    stripped, dropped = strip_media(body)
    content = stripped["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["_dropped"] == "image"
    assert content[2]["_dropped"] == "document"
    assert [d["path"] for d in dropped] == [
        "messages[0].content[1]",
        "messages[0].content[2]",
    ]
    assert dropped[0]["kind"] == "image"
    assert dropped[0]["media_type"] == "image/png"
    assert dropped[0]["bytes"] == len(base64.b64decode(image_data))
    assert dropped[0]["sha256"] == hashlib.sha256(base64.b64decode(image_data)).hexdigest()
    assert dropped[1]["media_type"] == "application/pdf"
    # Input is not mutated.
    assert body["messages"][0]["content"][1]["source"]["data"] == image_data


def test_image_nested_in_tool_result_is_stripped():
    body = {
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "content": [{
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": _png_b64(8)},
                }],
            }],
        }],
    }
    stripped, dropped = strip_media(body)
    block = stripped["messages"][0]["content"][0]["content"][0]
    assert block["_dropped"] == "image"
    assert dropped[0]["path"] == "messages[0].content[0].content[0]"
    assert dropped[0]["media_type"] == "image/jpeg"


def test_openai_chat_and_responses_media_shapes():
    png = b"\x89PNG" + b"q" * 32
    body = {
        "messages": [{
            "type": "image_url",
            "image_url": {"url": _data_url("image/png", png)},
        }],
        "input": [
            {"type": "input_image", "image_url": _data_url("image/webp", png)},
            {"type": "input_file", "file_data": base64.b64encode(b"%PDF").decode(), "media_type": "application/pdf"},
        ],
    }
    stripped, dropped = strip_media(body)
    assert stripped["messages"][0]["_dropped"] == "image"
    assert stripped["input"][0]["_dropped"] == "image"
    assert stripped["input"][1]["_dropped"] == "file"
    kinds = [d["kind"] for d in dropped]
    assert kinds == ["image", "image", "file"]
    assert dropped[0]["media_type"] == "image/png"
    assert dropped[1]["media_type"] == "image/webp"
    assert dropped[2]["media_type"] == "application/pdf"
    assert dropped[0]["bytes"] == len(png)


def test_remote_image_url_is_kept():
    body = {"messages": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}
    stripped, dropped = strip_media(body)
    assert stripped == body
    assert dropped == []


def test_data_url_fallback_respects_size_and_encoding():
    small = "data:image/png;base64," + base64.b64encode(b"tiny").decode()
    assert len(small) <= DATA_URL_MIN_CHARS
    huge_payload = b"z" * 5000
    huge = _data_url("image/png", huge_payload)
    assert len(huge) > DATA_URL_MIN_CHARS
    plain = "data:text/plain," + ("a" * 5000)
    body = {"note": small, "blob": huge, "text": plain, "nested": [huge]}
    stripped, dropped = strip_media(body)
    assert stripped["note"] == small
    assert stripped["blob"]["_dropped"] == "image"
    assert stripped["text"] == plain  # not base64, so not a media payload
    assert stripped["nested"][0]["_dropped"] == "image"
    assert [d["path"] for d in dropped] == ["blob", "nested[0]"]


def test_string_image_url_form_is_stripped():
    body = {"content": [{"type": "image_url", "image_url": _data_url("image/gif", b"GIF89a" + b"x" * 16)}]}
    stripped, dropped = strip_media(body)
    assert stripped["content"][0]["_dropped"] == "image"
    assert dropped[0]["media_type"] == "image/gif"


def test_strip_media_rejects_non_dict():
    with pytest.raises(TypeError):
        strip_media([1, 2, 3])  # type: ignore[arg-type]


# --- splitting and rebuild ---------------------------------------------------

def test_rebuild_matches_stripped_body_across_shapes():
    long_system = "s" * (BLOB_MIN_CHARS + 10)
    short_system = "short system"
    cases = [
        {"model": "m", "messages": [], "tools": []},
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
        {"system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "messages": []},
        {"system": long_system, "messages": [{"role": "user", "content": "q"}]},
        {"system": short_system, "temperature": 0.2},
        {"instructions": "i" * (BLOB_MIN_CHARS + 5), "input": [{"role": "user"}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "t"}, {"nested": [1, 2]}]}]},
    ]
    for body in cases:
        _assert_rebuilt(body)


def test_size_boundary_uses_canonical_json_length():
    # Exactly BLOB_MIN_CHARS stays inline; one more character becomes a blob.
    # A JSON string is quoted, so the raw text is two characters shorter.
    exact = "e" * (BLOB_MIN_CHARS - 2)
    assert len(canonical_json(exact)) == BLOB_MIN_CHARS
    over = exact + "e"
    params, blobs, _new = split_body({"system": exact, "instructions": over, "model": "m"})
    assert params["system"] == exact
    assert params["model"] == "m"
    assert "instructions" in blobs
    assert isinstance(blobs["instructions"], str)


def test_array_elements_are_individual_blobs_regardless_of_size():
    body = {"messages": [{"role": "user"}, {"role": "assistant"}], "tools": []}
    params, blobs, new_blobs = split_body(body)
    assert "messages" not in params
    assert len(blobs["messages"]) == 2
    assert blobs["tools"] == []
    assert len(new_blobs) == 2


def test_hash_ignores_key_order_and_is_stable_for_non_ascii():
    assert canonical_hash({"b": 1, "a": "你好"}) == canonical_hash({"a": "你好", "b": 1})
    emoji = {"content": "😀"}
    assert canonical_hash(emoji) == canonical_hash({"content": "😀"})
    # ensure_ascii=False: the digest covers the raw characters, not \\u escapes.
    escaped = json.dumps(emoji)
    assert "\\u" in escaped
    assert canonical_hash(emoji) != hashlib.sha256(escaped.encode()).hexdigest()


def test_duplicate_elements_share_one_blob_entry():
    message = {"role": "user", "content": "same"}
    _params, blobs, new_blobs = split_body({"messages": [message, message]})
    assert blobs["messages"][0] == blobs["messages"][1]
    assert len(new_blobs) == 1


def test_missing_blob_hash_is_reported():
    params, blobs, new_blobs = split_body({"messages": [{"role": "user"}]})
    digest = blobs["messages"][0]
    assert missing_blob_hashes(blobs, new_blobs) == []
    assert missing_blob_hashes(blobs, {}) == [digest]
    with pytest.raises(KeyError) as exc:
        rebuild_body(params, blobs, {})
    assert exc.value.args[0] == digest


# --- raw body classification -------------------------------------------------

@pytest.mark.parametrize("raw", [
    b"",
    b"not json",
    b"[1, 2, 3]",
    b'"just a string"',
    b"\xff\xfe binary",
])
def test_non_object_body_is_kept_raw_without_raising(raw: bytes):
    req, dropped, new_blobs = split_request_bytes(raw)
    assert req["params"] == {}
    assert req["blobs"] == {}
    assert req["raw"] == raw.decode("utf-8", errors="replace")
    assert dropped == []
    assert new_blobs == {}


def test_object_body_round_trips_through_bytes():
    body = {
        "model": "claude",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64()}},
            ]},
        ],
    }
    raw = json.dumps(body).encode()
    req, dropped, new_blobs = split_request_bytes(raw)
    assert req["raw"] is None
    assert len(dropped) == 1
    rebuilt = rebuild_body(req["params"], req["blobs"], new_blobs)
    stripped, _ = strip_media(body)
    assert canonical_json(rebuilt) == canonical_json(stripped)


# --- headers -----------------------------------------------------------------

def test_redact_headers_is_case_insensitive_and_merges_duplicates():
    headers = [
        (b"Authorization", b"Bearer secret"),
        (b"X-API-Key", b"k"),
        (b"Proxy-Authorization", b"p"),
        (b"Cookie", b"a=b"),
        (b"CF-Access-Client-Secret", b"s"),
        (b"User-Agent", b"claude-cli/1"),
        (b"X-Trace", b"one"),
        (b"X-Trace", b"two"),
    ]
    redacted = redact_headers(headers)
    for name in ("authorization", "x-api-key", "proxy-authorization", "cookie", "cf-access-client-secret"):
        assert redacted[name] == REDACTED
    assert redacted["user-agent"] == "claude-cli/1"
    assert redacted["x-trace"] == "one, two"
