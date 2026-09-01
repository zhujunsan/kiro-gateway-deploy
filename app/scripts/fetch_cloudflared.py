# app/scripts/fetch_cloudflared.py
"""Download the official cloudflared binary for the current or specified platform.

The version is pinned (not "latest") and every download is verified against a
known sha256, so builds are reproducible and tamper-evident. To bump: change
CLOUDFLARED_VERSION and refresh CLOUDFLARED_SHA256 with the values from the
release's checksum list.
"""
from __future__ import annotations

import hashlib
import io
import platform
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST_BASE = ROOT / "resources" / "cloudflared"

CLOUDFLARED_VERSION = "2026.8.3"
BASE_URL = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    f"{CLOUDFLARED_VERSION}"
)

# sha256 of each pinned release asset (the downloaded file, before extraction).
CLOUDFLARED_SHA256 = {
    # Note: 2026.8.3 release-body checksums for the two darwin .tgz assets are
    # wrong (same issue as 2026.7.x); values below are sha256 of the actual
    # GitHub release assets.
    "cloudflared-darwin-amd64.tgz": "61e1316266a00fd70ce40da011d612badc805367fb65293dd1925f938f704c99",
    "cloudflared-darwin-arm64.tgz": "40c9144d86df8937c5b43293a1f7d2d2107029aa74725023dd46b1b27154352f",
    "cloudflared-linux-amd64": "f29324fe934d1e100617484c78deef803c4dc2cd351d645bbde42e96b4fccc5e",
    "cloudflared-linux-arm64": "4bcfd35521a7cbc545ebfd5d57334a71ee180e2a64874981f374c81472118391",
    "cloudflared-windows-amd64.exe": "83e726ed18ea78c5ad5213c4c3a3a27051393950d2bc8ed4de69bec12d14eaae",
}

DOWNLOAD_TIMEOUT = 60  # seconds per attempt
DOWNLOAD_RETRIES = 3


def _download(url: str) -> bytes:
    """Download a URL with a per-attempt timeout and bounded retries."""
    last_err: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:  # noqa: S310
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            print(f"[warn] attempt {attempt}/{DOWNLOAD_RETRIES} failed: {e}")
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"failed to download {url} after {DOWNLOAD_RETRIES} attempts: {last_err}")


def _verify_sha256(filename: str, data: bytes) -> None:
    expected = CLOUDFLARED_SHA256.get(filename)
    if expected is None:
        raise RuntimeError(f"no pinned sha256 for {filename}; refusing to bundle unverified binary")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"sha256 mismatch for {filename}:\n  expected {expected}\n  actual   {actual}"
        )
    print(f"[ok] sha256 verified for {filename}")


def _target() -> tuple[str, str]:
    """Return (os_name, arch) matching cloudflared release asset naming."""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}[sysname]
    arch = {
        "x86_64": "amd64", "amd64": "amd64",
        "arm64": "arm64", "aarch64": "arm64",
    }[machine]
    return os_name, arch


def fetch(os_name: str, arch: str) -> Path:
    dest_dir = DEST_BASE / f"{os_name}-{arch}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if os_name == "windows":
        filename = "cloudflared-windows-amd64.exe"
        dest = dest_dir / "cloudflared.exe"
    elif os_name == "darwin":
        filename = f"cloudflared-darwin-{arch}.tgz"
        dest = dest_dir / "cloudflared"
    else:
        filename = f"cloudflared-linux-{arch}"
        dest = dest_dir / "cloudflared"

    url = f"{BASE_URL}/{filename}"
    print(f"downloading {url}")
    data = _download(url)
    _verify_sha256(filename, data)

    if filename.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = t.extractfile("cloudflared")
            dest.write_bytes(member.read())
    else:
        dest.write_bytes(data)

    if os_name != "windows":
        dest.chmod(0o755)

    print(f"[ok] {dest}")
    return dest


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--current-only"]
    if len(args) == 2:
        fetch(args[0], args[1])
    else:
        fetch(*_target())
