# app/packaging/macos_icon.py
"""macOS app-icon helpers for the PyInstaller build.

macOS 26 (Tahoe) introduced the layered "Liquid Glass" icon format authored
with Apple's Icon Composer and stored as an ``AppIcon.icon`` bundle. That
bundle is compiled by ``actool`` into an ``Assets.car`` asset catalog which the
system reads (via the ``CFBundleIconName`` Info.plist key) to render the
Default / Dark / Clear / Tinted appearances.

Older macOS versions ignore ``Assets.car`` and fall back to the classic
``icon.icns`` referenced by ``CFBundleIconFile`` — so we ship both.

This module:
  * compiles ``resources/AppIcon.icon`` -> ``Assets.car`` when ``actool`` is
    available (Xcode 26+ on macOS 26+), otherwise reuses the checked-in
    ``resources/Assets.car``;
  * installs ``Assets.car`` into ``<App>.app/Contents/Resources`` and makes
    sure the Info.plist carries ``CFBundleIconName``/``CFBundleIconFile``.

It is intentionally import-safe on non-macOS platforms (nothing here runs
unless explicitly called from the darwin build path).
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
RES = APP / "resources"

ICON_BUNDLE = RES / "AppIcon.icon"
ASSETS_CAR = RES / "Assets.car"
ICNS = RES / "icon.icns"

# Must match the .icon bundle stem and the CFBundleIconName value.
APP_ICON_NAME = "AppIcon"
# CFBundleIconFile fallback: PyInstaller copies BUNDLE(icon=icon.icns) into
# Contents/Resources under its original basename and sets CFBundleIconFile to
# "icon.icns" automatically. We only set this defensively if it is missing.
ICNS_RESOURCE_STEM = "icon.icns"


def compile_icon(out_dir: Path) -> Path | None:
    """Compile ``AppIcon.icon`` into ``out_dir/Assets.car`` using actool.

    Returns the path to the freshly compiled ``Assets.car`` on success, or
    ``None`` if actool is unavailable / the bundle is missing. Failures are
    non-fatal: the caller falls back to the checked-in ``Assets.car``.
    """
    if not ICON_BUNDLE.is_dir():
        return None
    actool = shutil.which("actool")
    if actool is None:
        try:
            actool = subprocess.run(
                ["xcrun", "--find", "actool"],
                capture_output=True, text=True, check=True,
            ).stdout.strip() or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            actool = None
    if not actool:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        actool, str(ICON_BUNDLE),
        "--compile", str(out_dir),
        "--app-icon", APP_ICON_NAME,
        "--include-all-app-icons",
        "--enable-on-demand-resources", "NO",
        "--development-region", "en",
        "--target-device", "mac",
        "--minimum-deployment-target", "26.0",
        "--platform", "macosx",
        "--output-partial-info-plist", str(out_dir / "partial.plist"),
        "--errors", "--warnings",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    car = out_dir / "Assets.car"
    return car if car.exists() else None


def install_into_app(app_path: Path) -> bool:
    """Place ``Assets.car`` into the .app and ensure the icon Info.plist keys.

    Tries to recompile the ``.icon`` bundle fresh (so the catalog matches the
    current toolchain); otherwise uses the committed ``resources/Assets.car``.
    Returns ``True`` if an ``Assets.car`` was installed.
    """
    resources = app_path / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)

    car = compile_icon(app_path.parent / "_icon_build")
    if car is None and ASSETS_CAR.exists():
        car = ASSETS_CAR

    installed = False
    if car is not None and car.exists():
        shutil.copy2(car, resources / "Assets.car")
        installed = True

    # Defensive: make sure the Info.plist carries both icon keys even if the
    # spec's info_plist was changed. CFBundleIconName drives macOS 26's
    # Liquid Glass rendering from Assets.car; CFBundleIconFile is the
    # pre-Tahoe .icns fallback.
    info = app_path / "Contents" / "Info.plist"
    if info.exists():
        with info.open("rb") as fh:
            plist = plistlib.load(fh)
        changed = False
        if installed and plist.get("CFBundleIconName") != APP_ICON_NAME:
            plist["CFBundleIconName"] = APP_ICON_NAME
            changed = True
        if not plist.get("CFBundleIconFile"):
            plist["CFBundleIconFile"] = ICNS_RESOURCE_STEM
            changed = True
        if changed:
            with info.open("wb") as fh:
                plistlib.dump(plist, fh)

    return installed
