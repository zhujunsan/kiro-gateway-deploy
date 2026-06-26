# app/tests/test_telemetry.py
"""Tests for the usage telemetry client (app/kiro_gateway_tray/telemetry.py).

Covers: aggregation, bucket alignment & closing, pending.jsonl spool/rewrite
idempotency, SSE + JSON usage extraction, endpoint filtering, ASGI request body
replay, model fallback, and the lifespan passthrough/flush path."""
from __future__ import annotations

import asyncio
import json

from kiro_gateway_tray import telemetry
from kiro_gateway_tray.telemetry import (
    Aggregator,
    PendingStore,
    Reporter,
    RequestSample,
    SecretRefresher,
    TelemetryConfig,
    TelemetryMiddleware,
    Uploader,
    bucket_start_for,
    extract_model,
    from_env,
    parse_usage,
)


# --- helpers ----------------------------------------------------------------

class FakeUploader(Uploader):
    """Uploader that records batches and returns a scripted outcome.

    ``outcome`` is one of UPLOAD_OK / UPLOAD_UNAUTHORIZED / UPLOAD_ERROR, or a
    callable(rows)->outcome for per-call scripting (e.g. 401 then OK)."""

    def __init__(self, ok: bool = True, outcome=None):
        super().__init__("https://example/telemetry", "secret")
        if outcome is None:
            outcome = telemetry.UPLOAD_OK if ok else telemetry.UPLOAD_ERROR
        self.outcome = outcome
        self.batches: list[list[dict]] = []
        self.secrets_used: list[str] = []

    def upload(self, rows):
        if not rows:
            return telemetry.UPLOAD_OK
        self.batches.append([dict(r) for r in rows])
        self.secrets_used.append(self.secret)
        if callable(self.outcome):
            return self.outcome(rows)
        return self.outcome


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _drive_http(middleware, *, method, path, body, response_messages,
                      capture):
    """Drive an ASGI request through the middleware with a mock inner app.

    ``capture`` collects the body the inner app actually receives (to assert
    replay). ``response_messages`` are the messages the inner app emits."""
    scope = {"type": "http", "method": method, "path": path, "headers": []}

    sent: list[dict] = []
    # request frames: split body in two to exercise more_body framing
    half = len(body) // 2
    frames = [
        {"type": "http.request", "body": body[:half], "more_body": True},
        {"type": "http.request", "body": body[half:], "more_body": False},
    ]
    idx = {"i": 0}

    async def receive():
        i = idx["i"]
        idx["i"] += 1
        return frames[i]

    async def send(message):
        sent.append(message)

    async def inner_app(scope, recv, snd):
        # Drain the (replayed) request body so we can assert it round-trips.
        chunks = bytearray()
        while True:
            msg = await recv()
            if msg["type"] == "http.request":
                chunks.extend(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
            else:
                break
        capture["body"] = bytes(chunks)
        for m in response_messages:
            await snd(m)

    middleware.app = inner_app
    await middleware(scope, receive, send)
    return sent


# --- config / from_env ------------------------------------------------------

def test_from_env_reads_all_fields():
    cfg = from_env({
        "TELEMETRY_URL": "https://w/telemetry",
        "TELEMETRY_SECRET": "sek",
        "TELEMETRY_USERNAME": "abc123",
        "APP_VERSION": "0.1.25",
        "TELEMETRY_BUCKET_SECONDS": "300",
    })
    assert cfg.endpoint_url == "https://w/telemetry"
    assert cfg.secret == "sek"
    assert cfg.username == "abc123"
    assert cfg.app_version == "0.1.25"
    assert cfg.bucket_seconds == 300
    assert cfg.enabled is True


def test_from_env_defaults_and_disabled():
    cfg = from_env({})
    assert cfg.enabled is False
    assert cfg.username == "unknown"
    assert cfg.app_version == "unknown"
    assert cfg.bucket_seconds == telemetry.DEFAULT_BUCKET_SECONDS


def test_from_env_bad_bucket_falls_back():
    cfg = from_env({"TELEMETRY_URL": "x", "TELEMETRY_BUCKET_SECONDS": "0"})
    assert cfg.bucket_seconds == telemetry.DEFAULT_BUCKET_SECONDS
    cfg = from_env({"TELEMETRY_URL": "x", "TELEMETRY_BUCKET_SECONDS": "junk"})
    assert cfg.bucket_seconds == telemetry.DEFAULT_BUCKET_SECONDS


# --- bucket alignment -------------------------------------------------------

def test_bucket_start_alignment():
    assert bucket_start_for(1000, 600) == 600
    assert bucket_start_for(1199, 600) == 600
    assert bucket_start_for(1200, 600) == 1200
    assert bucket_start_for(1234.9, 600) == 1200


# --- aggregation ------------------------------------------------------------

def test_aggregator_accumulates_same_bucket():
    agg = Aggregator("user", "0.1.0", 600)
    base = 1200.0
    agg.record(RequestSample(model="m", success=True, prompt_tokens=10,
                             completion_tokens=5, total_tokens=15,
                             request_bytes=100, response_bytes=200), now=base)
    agg.record(RequestSample(model="m", success=False, prompt_tokens=3,
                             completion_tokens=None, total_tokens=None,
                             request_bytes=50, response_bytes=10), now=base + 10)
    rows = agg.drain_all()
    assert len(rows) == 1
    r = rows[0]
    assert r["requests"] == 2
    assert r["successes"] == 1
    assert r["errors"] == 1
    assert r["prompt_tokens_sum"] == 13
    assert r["completion_tokens_sum"] == 5
    assert r["total_tokens_sum"] == 15
    assert r["request_bytes_sum"] == 150
    assert r["response_bytes_sum"] == 210
    assert r["bucket_start"] == 1200


def test_aggregator_splits_by_dimension():
    agg = Aggregator("user", "0.1.0", 600)
    base = 1200.0
    agg.record(RequestSample(model="m1", success=True), now=base)
    agg.record(RequestSample(model="m2", success=True), now=base)
    # different bucket
    agg.record(RequestSample(model="m1", success=True), now=base + 600)
    rows = agg.drain_all()
    assert len(rows) == 3


def test_collect_closed_only_returns_expired():
    agg = Aggregator("user", "0.1.0", 600)
    # open bucket at 1200 (closes at 1800)
    agg.record(RequestSample(model="m", success=True), now=1200)
    # now = 1700 -> not yet closed
    assert agg.collect_closed(now=1700) == []
    # now = 1800 -> closed
    closed = agg.collect_closed(now=1800)
    assert len(closed) == 1
    # drained, so a second collect is empty
    assert agg.collect_closed(now=1800) == []


# --- usage extraction -------------------------------------------------------

def test_parse_usage_openai():
    p, c, t = parse_usage({
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
    })
    assert (p, c, t) == (100, 20, 120)


def test_parse_usage_anthropic_input_output():
    p, c, t = parse_usage({
        "input_tokens": 30, "output_tokens": 7,
    })
    assert p == 30
    assert c == 7
    assert t == 37  # synthesised


def test_parse_usage_empty():
    assert parse_usage(None) == (None, None, None)


def test_sse_usage_extraction():
    usage = telemetry._merge_usage_from_sse(
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":50,'
        b'"completion_tokens":10,"total_tokens":60}}\n\n'
        b'data: [DONE]\n\n'
    )
    assert usage["prompt_tokens"] == 50
    assert usage["total_tokens"] == 60


def test_sse_usage_merges_anthropic_split():
    # Anthropic puts input under message.usage (message_start) and output under
    # a top-level usage (message_delta).
    usage = telemetry._merge_usage_from_sse(
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":40,"output_tokens":0}}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":12}}\n\n'
    )
    assert usage["input_tokens"] == 40
    assert usage["output_tokens"] == 12


def test_json_usage_extraction():
    usage = telemetry._usage_from_json(
        json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 1,
                              "total_tokens": 6}}).encode()
    )
    assert usage["prompt_tokens"] == 5


# --- model extraction -------------------------------------------------------

def test_extract_model_from_body():
    assert extract_model(b'{"model":"claude-sonnet-4","messages":[]}') == "claude-sonnet-4"


def test_extract_model_fallback_unknown():
    assert extract_model(b'not json') == "unknown"
    assert extract_model(b'{"messages":[]}') == "unknown"
    assert extract_model(b'') == "unknown"


# --- pending spool ----------------------------------------------------------

def test_pending_append_and_load(tmp_path):
    store = PendingStore(tmp_path / "telemetry" / "pending.jsonl")
    rows = [{"bucket_start": 1200, "username": "u", "model": "m",
             "app_version": "v", "requests": 1}]
    store.append(rows)
    store.append([{"bucket_start": 1800, "username": "u", "model": "m2",
                   "app_version": "v", "requests": 2}])
    loaded = store.load_all()
    assert len(loaded) == 2
    assert loaded[0]["bucket_start"] == 1200


def test_pending_rewrite_drops_expired(tmp_path):
    store = PendingStore(tmp_path / "pending.jsonl", max_retention_days=30)
    now = 100 * 86400.0
    fresh = {"bucket_start": int(now - 86400), "username": "u", "model": "m",
             "app_version": "v"}
    old = {"bucket_start": int(now - 40 * 86400), "username": "u", "model": "m2",
           "app_version": "v"}
    store.rewrite([fresh, old], now=now)
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0]["model"] == "m"


def test_pending_rewrite_empty_removes_file(tmp_path):
    store = PendingStore(tmp_path / "pending.jsonl")
    store.append([{"bucket_start": 1, "username": "u", "model": "m",
                   "app_version": "v"}])
    assert store.path.exists()
    store.rewrite([], now=1000)
    assert not store.path.exists()


def test_pending_skips_corrupt_lines(tmp_path):
    store = PendingStore(tmp_path / "pending.jsonl")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        '{"bucket_start":1,"username":"u","model":"m","app_version":"v"}\n'
        'garbage not json\n'
        '\n'
        '{"bucket_start":2,"username":"u","model":"n","app_version":"v"}\n',
        encoding="utf-8",
    )
    assert len(store.load_all()) == 2


# --- reporter upload / spool flow -------------------------------------------

def _reporter(tmp_path, *, ok=True, bucket_seconds=600):
    cfg = TelemetryConfig(endpoint_url="https://w", secret="s",
                          username="u", app_version="v",
                          bucket_seconds=bucket_seconds,
                          flush_interval=bucket_seconds)
    agg = Aggregator("u", "v", bucket_seconds)
    up = FakeUploader(ok=ok)
    pend = PendingStore(tmp_path / "pending.jsonl")
    return Reporter(cfg, agg, up, pend), up, pend


def test_tick_uploads_closed_bucket(tmp_path):
    rep, up, pend = _reporter(tmp_path)
    rep.aggregator.record(RequestSample(model="m", success=True), now=1200)
    rep.tick(now=1800)
    assert len(up.batches) == 1
    assert up.batches[0][0]["model"] == "m"
    assert pend.load_all() == []  # success ⇒ not spooled


def test_tick_failure_spools(tmp_path):
    rep, up, pend = _reporter(tmp_path, ok=False)
    rep.aggregator.record(RequestSample(model="m", success=True), now=1200)
    rep.tick(now=1800)
    loaded = pend.load_all()
    assert len(loaded) == 1
    assert loaded[0]["model"] == "m"


def test_pending_retry_idempotent_dedup(tmp_path):
    rep, up, pend = _reporter(tmp_path, ok=True)
    # Two spooled copies of the SAME bucket key (e.g. report-but-lost-response).
    row = {"bucket_start": 1200, "bucket_seconds": 600, "username": "u",
           "model": "m", "app_version": "v", "requests": 5}
    pend.append([row, dict(row)])
    rep._retry_pending(now=2000, force=True)
    # Deduped to a single row in the uploaded batch.
    assert len(up.batches) == 1
    assert len(up.batches[0]) == 1
    # Spool cleared on success.
    assert pend.load_all() == []


def test_pending_retry_keeps_on_failure(tmp_path):
    rep, up, pend = _reporter(tmp_path, ok=False)
    pend.append([{"bucket_start": 1200, "bucket_seconds": 600, "username": "u",
                  "model": "m", "app_version": "v"}])
    rep._retry_pending(now=2000, force=True)
    # Upload failed -> row stays spooled for next time.
    assert len(pend.load_all()) == 1


# --- secret refresh (401 -> /telemetry-secret -> retry, 60s throttle) -------

def _refresh_reporter(tmp_path, *, outcome, refresh_returns="new-secret",
                      throttle=60.0):
    """Reporter wired with a 401-then-OK uploader and a fake refresher."""
    cfg = TelemetryConfig(endpoint_url="https://w", secret="old-secret",
                          username="u", app_version="v",
                          provision_url="https://prov", shared_secret="act-code")
    agg = Aggregator("u", "v", 600)
    up = FakeUploader(outcome=outcome)
    up.secret = "old-secret"
    pend = PendingStore(tmp_path / "pending.jsonl")
    persisted = {"secret": None}
    refresh_calls = {"n": 0}

    def fake_refresh_fn():
        refresh_calls["n"] += 1
        return refresh_returns

    refresher = SecretRefresher(fake_refresh_fn, throttle_seconds=throttle)

    def on_refresh(s):
        persisted["secret"] = s

    rep = Reporter(cfg, agg, up, pend, refresher=refresher,
                   on_secret_refresh=on_refresh)
    return rep, up, pend, persisted, refresh_calls


def test_401_triggers_refresh_and_retry_succeeds(tmp_path):
    # First upload 401, second (after refresh) OK.
    calls = {"n": 0}

    def outcome(rows):
        calls["n"] += 1
        return telemetry.UPLOAD_UNAUTHORIZED if calls["n"] == 1 else telemetry.UPLOAD_OK

    rep, up, pend, persisted, refresh_calls = _refresh_reporter(
        tmp_path, outcome=outcome)
    rep.aggregator.record(RequestSample(model="m", success=True), now=1200)
    rep.tick(now=1800)

    assert refresh_calls["n"] == 1                 # refreshed once
    assert up.secret == "new-secret"               # uploader swapped key
    assert up.secrets_used == ["old-secret", "new-secret"]  # retried with new
    assert persisted["secret"] == "new-secret"     # persisted back to config
    assert pend.load_all() == []                   # retry succeeded ⇒ not spooled


def test_401_refresh_failure_spools(tmp_path):
    # Refresh returns "" (worker not configured / network); row must spool, not lost.
    rep, up, pend, persisted, refresh_calls = _refresh_reporter(
        tmp_path, outcome=telemetry.UPLOAD_UNAUTHORIZED, refresh_returns="")
    rep.aggregator.record(RequestSample(model="m", success=True), now=1200)
    rep.tick(now=1800)

    assert refresh_calls["n"] == 1
    assert up.secret == "old-secret"               # unchanged (refresh failed)
    assert persisted["secret"] is None
    assert len(pend.load_all()) == 1               # spooled, not dropped


def test_refresh_throttled_to_60s(tmp_path):
    # Two 401s across two ticks inside the throttle window -> only one refresh.
    rep, up, pend, persisted, refresh_calls = _refresh_reporter(
        tmp_path, outcome=telemetry.UPLOAD_UNAUTHORIZED, refresh_returns="")
    rep.aggregator.record(RequestSample(model="m1", success=True), now=1200)
    rep.tick(now=1800)                  # closes m1 -> 401 -> refresh attempt #1
    # A second closed bucket appears, ticked < 60s after the first refresh.
    rep.aggregator.record(RequestSample(model="m2", success=True), now=1000)
    rep.tick(now=1850)                  # closes m2 -> 401 -> throttled (no #2)

    assert refresh_calls["n"] == 1                 # throttled: no second refresh
    # both failed batches still spooled (nothing lost during throttle window)
    assert len(pend.load_all()) == 2


def test_refresh_allowed_after_throttle_window(tmp_path):
    rep, up, pend, persisted, refresh_calls = _refresh_reporter(
        tmp_path, outcome=telemetry.UPLOAD_UNAUTHORIZED, refresh_returns="")
    rep.refresher.maybe_refresh(now=1000.0)
    assert refresh_calls["n"] == 1
    # within window -> None (throttled), no new call
    assert rep.refresher.maybe_refresh(now=1030.0) is None
    assert refresh_calls["n"] == 1
    # past window -> attempts again
    rep.refresher.maybe_refresh(now=1061.0)
    assert refresh_calls["n"] == 2


def test_secret_refresher_returns_none_when_throttled():
    calls = {"n": 0}
    r = SecretRefresher(lambda: (calls.__setitem__("n", calls["n"] + 1) or "x"),
                        throttle_seconds=60.0)
    assert r.maybe_refresh(now=100.0) == "x"
    assert r.maybe_refresh(now=120.0) is None       # throttled
    assert r.maybe_refresh(now=161.0) == "x"
    assert calls["n"] == 2


def test_no_refresher_means_401_just_spools(tmp_path):
    # Reporter without a refresher (e.g. no activation code) must not crash on
    # 401 and must spool the rows.
    rep, up, pend = _reporter(tmp_path, ok=False)
    rep.uploader.outcome = telemetry.UPLOAD_UNAUTHORIZED
    assert rep.refresher is None
    rep.aggregator.record(RequestSample(model="m", success=True), now=1200)
    rep.tick(now=1800)
    assert len(pend.load_all()) == 1


def test_can_refresh_requires_url_and_secret_and_user():
    assert TelemetryConfig(provision_url="u", shared_secret="s",
                           username="x").can_refresh is True
    assert TelemetryConfig(provision_url="u", shared_secret="s",
                           username="").can_refresh is False
    assert TelemetryConfig(provision_url="", shared_secret="s",
                           username="x").can_refresh is False
    assert TelemetryConfig(provision_url="u", shared_secret="",
                           username="x").can_refresh is False


def test_from_env_reads_refresh_inputs():
    cfg = from_env({
        "TELEMETRY_URL": "https://w/telemetry",
        "TELEMETRY_PROVISION_URL": "https://prov",
        "TELEMETRY_SHARED_SECRET": "act-code",
        "TELEMETRY_USERNAME": "abc",
    })
    assert cfg.provision_url == "https://prov"
    assert cfg.shared_secret == "act-code"
    assert cfg.can_refresh is True


def test_flush_all_drains_open_buckets(tmp_path):
    rep, up, pend = _reporter(tmp_path, ok=True)
    # open bucket, not yet closed
    rep.aggregator.record(RequestSample(model="m", success=True), now=1700)
    rep.flush_all(now=1750)
    assert len(up.batches) == 1
    assert up.batches[0][0]["model"] == "m"


# --- middleware: endpoint filtering -----------------------------------------

def test_middleware_passes_through_non_collected(tmp_path):
    rep, up, pend = _reporter(tmp_path)
    mw = TelemetryMiddleware(None, rep)
    capture: dict = {}
    sent = _run(_drive_http(
        mw, method="GET", path="/health", body=b"",
        response_messages=[
            {"type": "http.response.start", "status": 200, "headers": []},
            {"type": "http.response.body", "body": b"ok", "more_body": False},
        ],
        capture=capture,
    ))
    # nothing recorded for /health
    assert rep.aggregator.drain_all() == []
    assert any(m["type"] == "http.response.start" for m in sent)


def test_middleware_ignores_get_on_collect_path(tmp_path):
    rep, _, _ = _reporter(tmp_path)
    mw = TelemetryMiddleware(None, rep)
    capture: dict = {}
    _run(_drive_http(
        mw, method="GET", path="/v1/chat/completions", body=b"",
        response_messages=[
            {"type": "http.response.start", "status": 200, "headers": []},
            {"type": "http.response.body", "body": b"x", "more_body": False},
        ],
        capture=capture,
    ))
    assert rep.aggregator.drain_all() == []


# --- middleware: body replay + JSON usage -----------------------------------

def test_middleware_replays_body_and_extracts_json_usage(tmp_path):
    rep, up, pend = _reporter(tmp_path)
    mw = TelemetryMiddleware(None, rep)
    body = json.dumps({"model": "claude-sonnet-4", "messages": [{"x": "y"}]}).encode()
    resp_json = json.dumps({
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    }).encode()
    capture: dict = {}
    _run(_drive_http(
        mw, method="POST", path="/v1/chat/completions", body=body,
        response_messages=[
            {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json")]},
            {"type": "http.response.body", "body": resp_json, "more_body": False},
        ],
        capture=capture,
    ))
    # body round-tripped to inner app despite us reading it first
    assert capture["body"] == body
    rows = rep.aggregator.drain_all()
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "claude-sonnet-4"
    assert r["requests"] == 1
    assert r["successes"] == 1
    assert r["prompt_tokens_sum"] == 11
    assert r["total_tokens_sum"] == 14
    assert r["request_bytes_sum"] == len(body)
    assert r["response_bytes_sum"] == len(resp_json)


# --- middleware: SSE usage --------------------------------------------------

def test_middleware_extracts_sse_usage(tmp_path):
    rep, up, pend = _reporter(tmp_path)
    mw = TelemetryMiddleware(None, rep)
    body = json.dumps({"model": "gpt-x", "stream": True}).encode()
    capture: dict = {}
    _run(_drive_http(
        mw, method="POST", path="/v1/chat/completions", body=body,
        response_messages=[
            {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"text/event-stream")]},
            {"type": "http.response.body",
             "body": b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
             "more_body": True},
            {"type": "http.response.body",
             "body": b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":7,'
                     b'"completion_tokens":2,"total_tokens":9,"credits_used":1.5}}\n\n',
             "more_body": True},
            {"type": "http.response.body", "body": b"data: [DONE]\n\n",
             "more_body": False},
        ],
        capture=capture,
    ))
    rows = rep.aggregator.drain_all()
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "gpt-x"
    assert r["prompt_tokens_sum"] == 7
    assert r["completion_tokens_sum"] == 2
    assert r["total_tokens_sum"] == 9
    assert r["successes"] == 1


def test_middleware_model_fallback_unknown(tmp_path):
    rep, _, _ = _reporter(tmp_path)
    mw = TelemetryMiddleware(None, rep)
    capture: dict = {}
    _run(_drive_http(
        mw, method="POST", path="/v1/messages", body=b'{"messages":[]}',
        response_messages=[
            {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json")]},
            {"type": "http.response.body", "body": b'{}', "more_body": False},
        ],
        capture=capture,
    ))
    rows = rep.aggregator.drain_all()
    assert rows[0]["model"] == "unknown"
    # no usage -> counted as a request but not a success
    assert rows[0]["requests"] == 1
    assert rows[0]["successes"] == 0


# --- middleware: lifespan passthrough + flush -------------------------------

def test_middleware_lifespan_passthrough_and_flush(tmp_path):
    rep, up, pend = _reporter(tmp_path)
    # seed an open bucket so shutdown flush has something to drain
    rep.aggregator.record(RequestSample(model="m", success=True), now=1700)

    started = {"v": False}
    orig_start = rep.start
    orig_flush = rep.flush_and_stop

    def fake_start():
        started["v"] = True
    rep.start = fake_start  # avoid spinning a real thread in the test
    flushed = {"v": False}

    def fake_flush():
        flushed["v"] = True
        orig_flush()  # exercises drain + upload
    rep.flush_and_stop = fake_flush

    mw = TelemetryMiddleware(None, rep)

    inner_sends: list[dict] = []

    async def inner_app(scope, recv, snd):
        assert scope["type"] == "lifespan"
        msg = await recv()
        assert msg["type"] == "lifespan.startup"
        await snd({"type": "lifespan.startup.complete"})
        msg = await recv()
        assert msg["type"] == "lifespan.shutdown"
        await snd({"type": "lifespan.shutdown.complete"})

    mw.app = inner_app

    queue = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    qi = {"i": 0}

    async def receive():
        m = queue[qi["i"]]
        qi["i"] += 1
        return m

    async def send(message):
        inner_sends.append(message)

    scope = {"type": "lifespan"}
    _run(mw(scope, receive, send))

    assert started["v"] is True
    assert flushed["v"] is True
    # the flush drained + uploaded the open bucket
    assert len(up.batches) == 1
    assert up.batches[0][0]["model"] == "m"
    # lifespan completion messages still forwarded to the server
    assert {m["type"] for m in inner_sends} == {
        "lifespan.startup.complete", "lifespan.shutdown.complete"
    }
