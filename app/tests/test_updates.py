# app/tests/test_updates.py
import json
import time

from kiro_gateway_tray import updates


def test_parse_version_strips_prefix():
    assert updates._parse_version("app-v0.2.0") == (0, 2, 0)
    assert updates._parse_version("v1.2.3") == (1, 2, 3)
    assert updates._parse_version("0.1.0") == (0, 1, 0)


def test_is_newer():
    assert updates._is_newer("0.1.0", "0.2.0") is True
    assert updates._is_newer("0.2.0", "0.2.0") is False
    assert updates._is_newer("0.2.0", "0.1.9") is False
    assert updates._is_newer("0.1.0", "1.0.0") is True


def test_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    # No cache yet -> should_check True
    assert updates._should_check() is True
    updates._write_cache(latest="0.2.0")
    # Just wrote -> within TTL -> should_check False
    assert updates._should_check() is False
    cached = updates._read_cache()
    assert cached["latest"] == "0.2.0"


def test_check_uses_cache_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="9.9.9")
    # fresh cache -> no HTTP call, returns cached latest
    def _boom(*a, **k):
        raise AssertionError("should not hit network when cache is fresh")
    monkeypatch.setattr(updates.httpx, "get", _boom)
    info = updates.check(current="0.1.0")
    assert info.latest == "9.9.9"
    assert info.update_available is True


def test_ttl_is_four_hours():
    # Update checks are throttled to once per 4 hours.
    assert updates._TTL_SECONDS == 4 * 60 * 60


def test_peek_cached_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    assert updates.peek_cached(current="0.1.0") is None


def test_peek_cached_newer_version(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v9.9.9")
    info = updates.peek_cached(current="0.1.0")
    assert info is not None
    assert info.latest == "v9.9.9"
    assert info.update_available is True


def test_peek_cached_same_version(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v0.2.0")
    assert updates.peek_cached(current="0.2.0") is None


def test_failed_fetch_bumps_checked_at(tmp_path, monkeypatch):
    # A failed fetch (e.g. 403 rate limit) must still mark the cache as checked
    # so we don't retry on every menu open and exhaust the API budget.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="0.1.0")
    # Force the next call to attempt a fetch, then make the fetch "fail".
    monkeypatch.setattr(updates, "_should_check", lambda: True)
    monkeypatch.setattr(updates, "_fetch_latest", lambda: None)

    before = time.time()
    updates.check(current="0.1.0")
    cached = updates._read_cache()
    assert cached["checked_at"] >= before
    # Restore real _should_check; cache should now look fresh.
    monkeypatch.undo()
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    assert updates._should_check() is False


def test_upgrade_forces_recheck(tmp_path, monkeypatch):
    # Cache written by an older app version must trigger a fresh check even
    # within the TTL window.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="0.1.0")
    cached = updates._read_cache()
    cached["app_version"] = "0.0.1"
    updates._cache_file().write_text(json.dumps(cached), encoding="utf-8")
    assert updates._should_check() is True


def test_version_status_no_cache_has_no_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    st = updates.version_status(current="0.1.0")
    assert st.latest is None
    assert st.upgradable is False


def test_version_status_upgradable(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v9.9.9")
    st = updates.version_status(current="0.1.0")
    assert st.latest == "v9.9.9"
    assert st.upgradable is True


def test_installer_filename_matches_published_assets():
    assert updates.installer_filename("v0.5.9", "darwin", "arm64") == (
        "KiroGatewayTray-0.5.9-macos-arm64.dmg"
    )
    assert updates.installer_filename("0.5.9", "Darwin", "x86_64") == (
        "KiroGatewayTray-0.5.9-macos-amd64.dmg"
    )
    assert updates.installer_filename("v0.5.9", "win32", "AMD64") == (
        "KiroGatewayTray-0.5.9-windows-amd64-setup.exe"
    )
    # No native Windows ARM build is published; that machine gets the amd64 exe.
    assert updates.installer_filename("v0.5.9", "Windows", "ARM64") == (
        "KiroGatewayTray-0.5.9-windows-amd64-setup.exe"
    )
    assert updates.installer_filename("v0.5.9", "linux", "x86_64") == (
        "kiro-gateway-tray-0.5.9-linux-x86_64.AppImage"
    )
    assert updates.installer_filename("v0.5.9", "Linux", "aarch64") == (
        "kiro-gateway-tray-0.5.9-linux-aarch64.AppImage"
    )
    # Linux sometimes reports arm64; the published AppImage is aarch64.
    assert updates.installer_filename("v0.5.9", "linux", "arm64") == (
        "kiro-gateway-tray-0.5.9-linux-aarch64.AppImage"
    )


def test_installer_filename_rejects_unknown_platform():
    assert updates.installer_filename("v1.2.3", "linux", "riscv64") is None
    assert updates.installer_filename("v1.2.3", "freebsd", "amd64") is None
    assert updates.installer_filename("", "darwin", "arm64") is None
    assert updates.installer_filename("   ", "darwin", "arm64") is None


def test_download_url_uses_versioned_asset():
    url = updates.download_url_for(
        "v1.2.3", repo="owner/repo", system="darwin", machine="arm64",
    )
    assert url == (
        "https://github.com/owner/repo/releases/download/"
        "v1.2.3/KiroGatewayTray-1.2.3-macos-arm64.dmg"
    )


def test_download_url_prefixes_bare_version():
    url = updates.download_url_for(
        "1.2.3", repo="owner/repo", system="win32", machine="AMD64",
    )
    assert url == (
        "https://github.com/owner/repo/releases/download/"
        "v1.2.3/KiroGatewayTray-1.2.3-windows-amd64-setup.exe"
    )


def test_download_url_none_for_unsupported_cpu():
    assert updates.download_url_for(
        "v1.2.3", system="linux", machine="riscv64",
    ) is None
    assert updates.download_url_for("", system="darwin", machine="arm64") is None


def test_peek_cached_includes_download_url(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(updates.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(updates.sys, "platform", "darwin")
    updates._write_cache(latest="v9.9.9")
    info = updates.peek_cached(current="0.1.0")
    assert info is not None
    assert info.download_url == (
        "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/"
        "v9.9.9/KiroGatewayTray-9.9.9-macos-arm64.dmg"
    )


def test_check_cache_includes_download_url(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(updates.sys, "platform", "linux")
    updates._write_cache(latest="v9.9.9")

    def _boom(*a, **k):
        raise AssertionError("should not hit network when cache is fresh")

    monkeypatch.setattr(updates.httpx, "get", _boom)
    info = updates.check(current="0.1.0")
    assert info.download_url == (
        "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/"
        "v9.9.9/kiro-gateway-tray-9.9.9-linux-x86_64.AppImage"
    )


def test_installer_names_match_packaging_helpers():
    # Keep the click-to-download names locked to the files CI actually publishes.
    import sys
    from pathlib import Path

    packaging = Path(__file__).resolve().parents[1] / "packaging"
    sys.path.insert(0, str(packaging))
    from target_arch import linux_appimage_filename

    for machine in ("x86_64", "amd64", "aarch64", "arm64", "AMD64", "ARM64"):
        assert updates.installer_filename("1.2.3", "linux", machine) == (
            linux_appimage_filename("1.2.3", machine)
        )
    windows = (Path(__file__).resolve().parents[1] / "packaging" / "make_dist.py").read_text(
        encoding="utf-8"
    )
    assert 'f"KiroGatewayTray-{VER}-windows-amd64-setup.exe"' in windows
    assert updates.installer_filename("1.2.3", "win32", "AMD64") == (
        "KiroGatewayTray-1.2.3-windows-amd64-setup.exe"
    )


def test_version_status_not_upgradable_when_ahead_of_release(tmp_path, monkeypatch):
    # Cache left over from before an upgrade points at an older release than
    # the running app. Must not read as upgradable (no "高于发布版" confusion).
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v0.1.17")
    st = updates.version_status(current="0.1.23")
    assert st.latest == "v0.1.17"
    assert st.upgradable is False
