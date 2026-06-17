# app/packaging/make_dist.py
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DIST = APP / "dist"
OUT = APP / "release"
sys.path.insert(0, str(APP))
from kiro_gateway_tray import __version__ as VER  # noqa: E402


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_sha(out: Path) -> str:
    digest = _sha256(out)
    (out.parent / (out.name + ".sha256")).write_text(f"{digest}  {out.name}\n")
    return digest


def build_macos_dmg() -> Path:
    arch = "arm64" if platform.machine() == "arm64" else "amd64"
    out = OUT / f"KiroGatewayTray-{VER}-macos-{arch}.dmg"
    result = subprocess.run(
        [
            "create-dmg",
            "--volname", "KiroGatewayTray",
            "--window-pos", "200", "120",
            "--window-size", "560", "400",
            "--icon-size", "120",
            "--icon", "KiroGatewayTray.app", "140", "190",
            "--hide-extension", "KiroGatewayTray.app",
            "--app-drop-link", "420", "190",
            str(out),
            str(DIST),
        ],
        check=False,
    )
    # create-dmg exits 2 when no background image is set – DMG is still valid
    if result.returncode not in (0, 2):
        raise subprocess.CalledProcessError(result.returncode, "create-dmg")
    return out


def build_windows_installer() -> Path:
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    iscc = next((p for p in candidates if p.exists()), None)
    if iscc is None:
        raise FileNotFoundError("ISCC.exe not found; install Inno Setup 6")

    OUT.mkdir(exist_ok=True)
    subprocess.run(
        [
            str(iscc),
            f"/DAppVersion={VER}",
            f"/DDistDir={DIST / 'KiroGatewayTray'}",
            f"/DOutputDir={OUT}",
            str(APP / "packaging" / "kiro_gateway_tray.iss"),
        ],
        check=True,
    )
    return OUT / f"KiroGatewayTray-{VER}-windows-amd64-setup.exe"


def build_linux_appimage() -> Path:
    out = OUT / f"kiro-gateway-tray-{VER}-linux-x86_64.AppImage"
    build_dir = APP / "build"

    appdir = build_dir / "kiro-gateway-tray.AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)

    # Copy entire PyInstaller bundle into AppDir
    shutil.copytree(DIST / "kiro-gateway-tray", appdir / "kiro-gateway-tray")

    # AppRun entry point
    apprun = appdir / "AppRun"
    apprun.write_text('#!/bin/sh\nexec "$APPDIR/kiro-gateway-tray/kiro-gateway-tray" "$@"\n')
    apprun.chmod(0o755)

    # .desktop file (required by AppImage spec)
    (appdir / "kiro-gateway-tray.desktop").write_text(
        "[Desktop Entry]\n"
        "Name=Kiro Tray\n"
        "Exec=kiro-gateway-tray\n"
        "Icon=kiro-gateway-tray\n"
        "Type=Application\n"
        "Categories=Utility;Network;\n"
        "Comment=Kiro gateway tray app\n"
    )

    # Icon
    icon_src = APP / "resources" / "icon.png"
    if icon_src.exists():
        shutil.copy(icon_src, appdir / "kiro-gateway-tray.png")
        shutil.copy(icon_src, appdir / ".DirIcon")

    # appimagetool downloaded by CI to app/build/
    appimagetool = build_dir / "appimagetool-x86_64.AppImage"
    if not appimagetool.exists():
        raise FileNotFoundError(
            f"appimagetool not found at {appimagetool}; "
            "CI should have downloaded it via fetch_appimagetool step"
        )

    env = {**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"}
    subprocess.run(
        [str(appimagetool), str(appdir), str(out)],
        check=True,
        env=env,
    )
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)

    if sys.platform == "darwin":
        out = build_macos_dmg()
    elif sys.platform.startswith("win"):
        out = build_windows_installer()
    else:
        out = build_linux_appimage()

    digest = _write_sha(out)
    print(f"[ok] {out.name}  sha256={digest}")


if __name__ == "__main__":
    main()
