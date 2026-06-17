# app/packaging/make_dist.py
from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DIST = APP / "dist"
OUT = APP / "release"
sys.path.insert(0, str(APP))
from kiro_tray import __version__ as VER  # noqa: E402


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        arch = "arm64" if platform.machine() == "arm64" else "amd64"
        out = OUT / f"KiroTray-{VER}-macos-{arch}.zip"
        shutil.make_archive(str(out.with_suffix("")), "zip", DIST, "KiroTray.app")
    elif sys.platform.startswith("win"):
        out = OUT / f"KiroTray-{VER}-windows-amd64.zip"
        shutil.make_archive(str(out.with_suffix("")), "zip", DIST, "KiroTray")
    else:
        out = OUT / f"kiro-tray-{VER}-linux-amd64.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(DIST / "kiro-tray", arcname="kiro-tray")

    digest = _sha256(out)
    (out.parent / (out.name + ".sha256")).write_text(f"{digest}  {out.name}\n")
    print(f"[ok] {out.name}  sha256={digest}")


if __name__ == "__main__":
    main()
