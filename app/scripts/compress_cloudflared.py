# app/scripts/compress_cloudflared.py
"""UPX-compress the bundled cloudflared binary to shrink packaged artifacts.

cloudflared runs as a standalone child process (see kiro_gateway_tray.cloudflared),
so compressing it does not affect the PyInstaller-frozen Python runtime. macOS is
skipped on purpose: UPX support for Apple Silicon Mach-O is unreliable and would
break the binary's existing code signature.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST_BASE = ROOT / "resources" / "cloudflared"


def _target() -> tuple[str, str]:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}[sysname]
    arch = {
        "x86_64": "amd64", "amd64": "amd64",
        "arm64": "arm64", "aarch64": "arm64",
    }[machine]
    return os_name, arch


def _binary_path(os_name: str, arch: str) -> Path:
    name = "cloudflared.exe" if os_name == "windows" else "cloudflared"
    return DEST_BASE / f"{os_name}-{arch}" / name


def compress(binary: Path) -> bool:
    """Compress `binary` in place with UPX. Returns True if it is packed afterwards."""
    upx = shutil.which("upx")
    if upx is None:
        print("[skip] upx not found on PATH; leaving cloudflared uncompressed")
        return False

    # `upx -t` succeeds on an already-packed binary; treat that as done (idempotent).
    if subprocess.run([upx, "-t", str(binary)], capture_output=True).returncode == 0:
        print(f"[skip] already UPX-packed: {binary}")
        return True

    before = binary.stat().st_size
    result = subprocess.run([upx, "--best", "--lzma", str(binary)], capture_output=True, text=True)
    if result.returncode != 0:
        # AlreadyPackedException (2) is benign; anything else is a real failure.
        if "AlreadyPackedException" in (result.stderr + result.stdout):
            print(f"[skip] already UPX-packed: {binary}")
            return True
        sys.stderr.write(result.stdout + result.stderr)
        raise RuntimeError(f"upx failed on {binary} (exit {result.returncode})")

    after = binary.stat().st_size
    pct = (1 - after / before) * 100 if before else 0
    print(f"[ok] {binary.name}: {before:,} -> {after:,} bytes ({pct:.1f}% smaller)")
    return True


def main() -> None:
    os_name, arch = _target()
    if os_name == "darwin":
        print("[skip] macOS: UPX is unreliable on Apple Silicon and breaks signatures")
        return

    binary = _binary_path(os_name, arch)
    if not binary.exists():
        raise FileNotFoundError(
            f"cloudflared binary not found at {binary}; run scripts/fetch_cloudflared.py first"
        )
    compress(binary)


if __name__ == "__main__":
    main()
