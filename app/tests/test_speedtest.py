# app/tests/test_speedtest.py
"""Tests for the speed-test side-channel (app/kiro_gateway_tray/speedtest.py).

Covers: route matching + passthrough, Bearer/query auth (including
constant-time reject), download sizing & cap, upload byte counting, the HTML
page, and the wrap_app enable/disable switch."""
from __future__ import annotations

import asyncio
import json

from kiro_gateway_tray import speedtest
from kiro_gateway_tray.speedtest import SpeedTestMiddleware, wrap_app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _drive(middleware, *, method="GET", path="/speedtest/ping",
                 headers=None, query=b"", body=b"", inner=None):
    """Drive one ASGI request through the middleware. Returns the sent messages.

    ``headers`` is a list of (bytes, bytes). ``inner`` overrides the pass-through
    app (defaults to a sentinel that records it was reached)."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": headers or [],
    }
    frames = [{"type": "http.request", "body": body, "more_body": False}]
    idx = {"i": 0}

    async def receive():
        i = idx["i"]
        idx["i"] = min(i + 1, len(frames) - 1)
        return frames[i]

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    reached = {"inner": False}

    async def default_inner(scope, recv, snd):
        reached["inner"] = True
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"INNER"})

    middleware.app = inner or default_inner
    await middleware(scope, receive, send)
    return sent, reached


def _body(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _status(sent: list[dict]) -> int:
    for m in sent:
        if m["type"] == "http.response.start":
            return m["status"]
    return 0


def _headers(sent: list[dict]) -> dict[bytes, bytes]:
    for m in sent:
        if m["type"] == "http.response.start":
            return {k.lower(): v for k, v in m.get("headers", [])}
    return {}


# --- routing / passthrough ---------------------------------------------------

def test_non_speedtest_path_passes_through():
    mw = SpeedTestMiddleware(None, "k")
    sent, reached = _run(_drive(mw, path="/v1/chat/completions", method="POST"))
    assert reached["inner"] is True
    assert _body(sent) == b"INNER"


def test_non_http_scope_passes_through():
    mw = SpeedTestMiddleware(None, "k")
    hit = {"n": 0}

    async def inner(scope, recv, snd):
        hit["n"] += 1

    async def recv():
        return {}

    async def snd(m):
        pass

    mw.app = inner
    _run(mw({"type": "lifespan"}, recv, snd))
    assert hit["n"] == 1


# --- auth --------------------------------------------------------------------

def test_ping_requires_auth():
    mw = SpeedTestMiddleware(None, "secret")
    sent, _ = _run(_drive(mw, path="/speedtest/ping"))
    assert _status(sent) == 401


def test_ping_accepts_bearer():
    mw = SpeedTestMiddleware(None, "secret")
    sent, _ = _run(_drive(
        mw, path="/speedtest/ping",
        headers=[(b"authorization", b"Bearer secret")],
    ))
    assert _status(sent) == 200
    assert json.loads(_body(sent))["pong"] is True


def test_ping_accepts_query_key():
    mw = SpeedTestMiddleware(None, "secret")
    sent, _ = _run(_drive(mw, path="/speedtest/ping", query=b"key=secret"))
    assert _status(sent) == 200


def test_wrong_key_rejected():
    mw = SpeedTestMiddleware(None, "secret")
    sent, _ = _run(_drive(
        mw, path="/speedtest/ping",
        headers=[(b"authorization", b"Bearer nope")],
    ))
    assert _status(sent) == 401


def test_empty_configured_key_fails_closed():
    mw = SpeedTestMiddleware(None, "")
    sent, _ = _run(_drive(mw, path="/speedtest/ping", query=b"key="))
    assert _status(sent) == 401


# --- download ----------------------------------------------------------------

def test_download_streams_requested_bytes():
    mw = SpeedTestMiddleware(None, "k")
    sent, _ = _run(_drive(
        mw, path="/speedtest/download", query=b"bytes=1000&key=k",
    ))
    assert _status(sent) == 200
    assert len(_body(sent)) == 1000
    assert _headers(sent)[b"content-length"] == b"1000"
    assert b"no-transform" in _headers(sent)[b"cache-control"]


def test_download_caps_huge_request():
    mw = SpeedTestMiddleware(None, "k")
    huge = str(speedtest._MAX_DOWNLOAD * 10).encode()
    sent, _ = _run(_drive(
        mw, path="/speedtest/download", query=b"bytes=" + huge + b"&key=k",
    ))
    assert len(_body(sent)) == speedtest._MAX_DOWNLOAD


def test_download_bad_bytes_uses_default():
    mw = SpeedTestMiddleware(None, "k")
    sent, _ = _run(_drive(
        mw, path="/speedtest/download", query=b"bytes=junk&key=k",
    ))
    assert len(_body(sent)) == speedtest._DEFAULT_DOWNLOAD


# --- upload ------------------------------------------------------------------

def test_upload_counts_bytes():
    mw = SpeedTestMiddleware(None, "k")
    sent, _ = _run(_drive(
        mw, path="/speedtest/upload", method="POST",
        query=b"key=k", body=b"x" * 4096,
    ))
    assert _status(sent) == 200
    j = json.loads(_body(sent))
    assert j["received_bytes"] == 4096
    assert j["capped"] is False


def test_upload_requires_auth():
    mw = SpeedTestMiddleware(None, "k")
    sent, _ = _run(_drive(
        mw, path="/speedtest/upload", method="POST", body=b"data",
    ))
    assert _status(sent) == 401


# --- HTML page ---------------------------------------------------------------

def test_page_served_without_auth():
    mw = SpeedTestMiddleware(None, "k")
    sent, _ = _run(_drive(mw, path="/speedtest", method="GET"))
    assert _status(sent) == 200
    assert _headers(sent)[b"content-type"].startswith(b"text/html")
    assert b"<!doctype html>" in _body(sent).lower()


def test_trailing_slash_serves_page():
    mw = SpeedTestMiddleware(None, "k")
    sent, _ = _run(_drive(mw, path="/speedtest/", method="GET"))
    assert _status(sent) == 200
    assert b"html" in _body(sent).lower()


def test_unknown_speedtest_route_404():
    mw = SpeedTestMiddleware(None, "k")
    sent, _ = _run(_drive(mw, path="/speedtest/bogus", query=b"key=k"))
    assert _status(sent) == 404


# --- wrap_app ----------------------------------------------------------------

def test_wrap_app_enabled_by_default():
    sentinel = object()
    wrapped = wrap_app(sentinel, env={"PROXY_API_KEY": "k"})
    assert isinstance(wrapped, SpeedTestMiddleware)
    assert wrapped.api_key == "k"
    assert wrapped.app is sentinel


def test_wrap_app_disabled_returns_app_unchanged():
    sentinel = object()
    wrapped = wrap_app(sentinel, env={"SPEEDTEST_ENABLED": "false", "PROXY_API_KEY": "k"})
    assert wrapped is sentinel
