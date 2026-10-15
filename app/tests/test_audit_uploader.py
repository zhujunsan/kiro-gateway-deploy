"""Cloud config pull and OSS upload for sealed audit batches."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from kiro_gateway_tray.appconfig import AppCfg
from kiro_gateway_tray.audit_uploader import AuditUploader


def _cfg(mode_override: str = "") -> AppCfg:
    cfg = AppCfg()
    cfg.cloudflare.provision_url = "https://worker.test"
    cfg.cloudflare.shared_secret = "secret"
    cfg.audit.mode_override = mode_override
    return cfg


def _batch(root: Path, name: str = "20260930T120000Z-abcd1234") -> tuple[Path, Path]:
    dest = root / "outbox" / "26-09-30" / "alice"
    dest.mkdir(parents=True)
    blob = dest / f"{name}.blob.jsonl.zst"
    req = dest / f"{name}.req.jsonl.zst"
    blob.write_bytes(b"blob-bytes")
    req.write_bytes(b"req-bytes")
    return blob, req


def _uploader(tmp_path: Path, handler, cfg: AppCfg, box: list[float]) -> AuditUploader:
    transport = httpx.MockTransport(handler)
    return AuditUploader(
        tmp_path,
        clock=lambda: box[0],
        config_provider=lambda: cfg,
        username_provider=lambda: "alice",
        transport=transport,
    )


def test_success_and_conflict_delete_the_local_file(tmp_path):
    blob, req = _batch(tmp_path)
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client-config":
            return httpx.Response(200, json={"ok": True, "audit": {"mode": "off"}})
        if request.url.path == "/audit/upload-url":
            body = json.loads(request.content)
            uploads = []
            for key in body["keys"]:
                status = 409 if key.endswith(".blob.jsonl.zst") else 200
                uploads.append({
                    "key": key,
                    "url": f"https://oss.test/{key}",
                    "headers": {"content-type": "application/octet-stream", "x-oss-forbid-overwrite": "true"},
                })
                puts.append(f"sign:{status}:{key}")
            # stash statuses on the urls via the key itself; PUT below branches on path
            return httpx.Response(200, json={"ok": True, "uploads": uploads})
        if request.method == "PUT":
            puts.append("put:" + request.url.path)
            code = 409 if request.url.path.endswith(".blob.jsonl.zst") else 200
            assert request.headers["x-oss-forbid-overwrite"] == "true"
            return httpx.Response(code)
        return httpx.Response(500)

    box = [1_000.0]
    _uploader(tmp_path, handler, _cfg(), box).tick()
    assert not blob.exists()
    assert not req.exists()
    assert any(item.startswith("put:") for item in puts)


def test_forbidden_then_server_error_keeps_the_file_and_backs_off(tmp_path):
    _batch(tmp_path)
    calls = {"url": 0, "put": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client-config":
            return httpx.Response(200, json={"ok": True, "audit": {"mode": "off"}})
        if request.url.path == "/audit/upload-url":
            calls["url"] += 1
            body = json.loads(request.content)
            code = 403 if calls["url"] == 1 else 500
            if code == 403:
                uploads = [{
                    "key": key,
                    "url": f"https://oss.test/{key}",
                    "headers": {"x-oss-forbid-overwrite": "true"},
                } for key in body["keys"]]
                return httpx.Response(200, json={"ok": True, "uploads": uploads})
            return httpx.Response(500, json={"error": "nope"})
        if request.method == "PUT":
            calls["put"] += 1
            return httpx.Response(403)
        return httpx.Response(500)

    box = [1_000.0]
    up = _uploader(tmp_path, handler, _cfg(), box)
    up.tick()  # signs, PUT 403, file stays, backoff 30s
    assert calls == {"url": 1, "put": 1}
    assert list((tmp_path / "outbox").rglob("*.zst"))
    up.tick()  # still inside the backoff window
    assert calls["url"] == 1
    box[0] += 31
    up.tick()  # upload-url itself returns 500; file still there
    assert calls["url"] == 2
    assert calls["put"] == 1
    assert list((tmp_path / "outbox").rglob("*.zst"))


def test_storage_not_configured_does_not_retry_immediately(tmp_path):
    _batch(tmp_path)
    calls = {"url": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client-config":
            return httpx.Response(200, json={"ok": True, "audit": {"mode": "off"}})
        if request.url.path == "/audit/upload-url":
            calls["url"] += 1
            return httpx.Response(503, json={"error": "audit storage not configured"})
        return httpx.Response(500)

    box = [5_000.0]
    up = _uploader(tmp_path, handler, _cfg(), box)
    up.tick()
    up.tick()
    assert calls["url"] == 1
    assert list((tmp_path / "outbox").rglob("*.zst"))


def test_blob_success_req_failure_retries_only_the_request(tmp_path):
    blob, req = _batch(tmp_path)
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client-config":
            return httpx.Response(200, json={"ok": True, "audit": {"mode": "off"}})
        if request.url.path == "/audit/upload-url":
            body = json.loads(request.content)
            seen.append(body["keys"])
            return httpx.Response(200, json={"ok": True, "uploads": [
                {"key": key, "url": f"https://oss.test/{key}", "headers": {"x-oss-forbid-overwrite": "true"}}
                for key in body["keys"]
            ]})
        if request.method == "PUT":
            code = 200 if request.url.path.endswith(".blob.jsonl.zst") else 500
            return httpx.Response(code)
        return httpx.Response(500)

    box = [1_000.0]
    up = _uploader(tmp_path, handler, _cfg(), box)
    up.tick()
    assert not blob.exists()
    assert req.exists()
    box[0] += 31
    # Second round: req still fails, so it is requested again and only that key.
    up.tick()
    assert seen[0][0].endswith(".blob.jsonl.zst")
    assert seen[1] == [seen[0][1]]
    assert req.exists()


def test_cloud_mode_is_published_unless_locally_overridden(tmp_path):
    modes = iter(["best_effort", "off"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client-config":
            return httpx.Response(200, json={"ok": True, "audit": {"mode": next(modes), "rotate_seconds": 600}})
        return httpx.Response(500)

    box = [1_000.0]
    cfg = _cfg()
    up = _uploader(tmp_path, handler, cfg, box)
    up.tick()
    control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert control["mode"] == "best_effort"

    box[0] += 600
    up.tick()
    control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert control["mode"] == "off"

    box[0] += 600
    cfg.audit.mode_override = "off"
    # Cloud would say best_effort again, but the iterator is exhausted; pin it.
    def handler_on(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client-config":
            return httpx.Response(200, json={"ok": True, "audit": {"mode": "best_effort"}})
        return httpx.Response(500)

    up2 = _uploader(tmp_path, handler_on, cfg, box)
    up2.tick()
    control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert control["mode"] == "off"
