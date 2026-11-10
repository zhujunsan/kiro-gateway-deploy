# app/packaging/make_dist.py
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DIST = APP / "dist"
OUT = APP / "release"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "packaging"))
from kiro_gateway_tray import __version__ as VER  # noqa: E402
from target_arch import (  # noqa: E402
    appimage_arch,
    appimagetool_filename,
    deb_arch,
    linux_appimage_filename,
    linux_deb_filename,
)

_LINUX_BINARY = "kiro-gateway-tray"
_DEB_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+:~-]*$")
_DEB_ARCHITECTURES = frozenset({"amd64", "arm64"})


def _codesign_adhoc(app_path: Path) -> None:
    """Ad-hoc (self) sign the staged .app in place.

    On Apple Silicon macOS *requires* a valid code signature; a bundle with no
    signature plus the com.apple.quarantine attribute is reported by Gatekeeper
    as "damaged and can't be opened". We have no Apple Developer cert, so we
    sign ad-hoc (identity "-"): the signature is valid but not from an
    identified developer, which downgrades the hard "damaged" block to the
    milder "unidentified developer" prompt (right-click > Open).

    Must run *after* the icon catalog is installed and right before create-dmg,
    because rewriting Contents/Resources would invalidate an earlier signature.
    """
    if not shutil.which("codesign"):
        print("[warn] codesign not found; skipping ad-hoc signing")
        return

    # Sign nested executables/dylibs first so the bundle seal is consistent.
    # --deep usually covers these, but the bundled cloudflared is a loose
    # Mach-O child process, so sign it explicitly to be robust.
    inner: list[Path] = []
    for sub in ("Contents/Frameworks", "Contents/Resources", "Contents/MacOS"):
        d = app_path / sub
        if d.exists():
            inner += [p for p in d.rglob("cloudflared") if p.is_file()]
    for binary in inner:
        subprocess.run(
            ["codesign", "--force", "--sign", "-", "--timestamp=none", str(binary)],
            check=False,
        )

    result = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", str(app_path)],
        check=False,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "codesign")

    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_path)],
        check=False,
    )
    if verify.returncode != 0:
        print(f"[warn] codesign verify reported issues (rc={verify.returncode})")
    else:
        print(f"[ok] ad-hoc signed {app_path.name}")


def build_macos_dmg() -> Path:
    arch = "arm64" if platform.machine() == "arm64" else "amd64"
    out = OUT / f"KiroGatewayTray-{VER}-macos-{arch}.dmg"

    # create-dmg puts everything in the source folder into the DMG.
    # DIST contains both the raw COLLECT folder and the .app bundle;
    # stage only the .app into a temp dir to avoid the extra folder.
    stage = APP / "build" / "dmg-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(DIST / "KiroGatewayTray.app", stage / "KiroGatewayTray.app", symlinks=True)

    # Belt-and-suspenders: ensure the macOS 26 Liquid Glass icon catalog
    # (Assets.car) and CFBundleIconName are present in the staged .app. The
    # spec already does this post-BUNDLE, but re-running here keeps the DMG
    # correct even if the .app was produced by an older spec or copied in.
    try:
        sys.path.insert(0, str(APP / "packaging"))
        import macos_icon  # noqa: E402

        macos_icon.install_into_app(stage / "KiroGatewayTray.app")
    except Exception as exc:  # non-fatal: fall back to whatever the .app has
        print(f"[warn] macOS icon catalog step skipped: {exc}")

    # Ad-hoc sign AFTER the icon catalog is installed (rewriting Resources would
    # invalidate the seal) and BEFORE create-dmg packages the bundle. Eliminates
    # the Apple Silicon "damaged and can't be opened" Gatekeeper error.
    _codesign_adhoc(stage / "KiroGatewayTray.app")

    result = subprocess.run(
        [
            "create-dmg",
            "--volname", "KiroGatewayTray",
            # UDBZ (bzip2) compresses noticeably better than the default UDZO
            # (zlib), shrinking the shipped .dmg. Only affects download size;
            # the installed .app is identical after copy.
            "--format", "UDBZ",
            "--window-pos", "200", "120",
            "--window-size", "560", "400",
            "--icon-size", "120",
            "--icon", "KiroGatewayTray.app", "140", "190",
            "--hide-extension", "KiroGatewayTray.app",
            "--app-drop-link", "420", "190",
            str(out),
            str(stage),
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


def render_desktop_entry(exec_path: str) -> str:
    """Return a ``.desktop`` file that launches ``exec_path``.

    Args:
        exec_path: Freedesktop ``Exec`` value. AppImage keeps the bare binary
            name; the Debian package uses the absolute ``/usr/bin`` wrapper.

    Returns:
        Desktop-entry text, including the trailing newline.

    Raises:
        ValueError: ``exec_path`` is empty or contains a newline.
    """
    if not exec_path or any(char in exec_path for char in "\n\r"):
        raise ValueError(f"invalid desktop Exec value: {exec_path!r}")
    return (
        "[Desktop Entry]\n"
        "Name=Kiro Gateway Tray\n"
        f"Exec={exec_path}\n"
        "Icon=kiro-gateway-tray\n"
        "Type=Application\n"
        "Categories=Utility;Network;\n"
        "Comment=Kiro gateway tray app\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
    )


def render_deb_control(version: str, arch: str) -> str:
    """Return a Debian ``control`` file for this version and architecture.

    Args:
        version: Debian upstream version. Digits, letters, and ``. + ~ -``.
        arch: ``amd64`` or ``arm64``.

    Returns:
        Control-file text, including the trailing newline.

    Raises:
        ValueError: ``version`` or ``arch`` cannot appear in a Debian control file.
    """
    if not _DEB_VERSION_RE.fullmatch(version):
        raise ValueError(
            f"version {version!r} cannot be used in a Debian package; "
            "use digits, letters, and . + ~ -"
        )
    if arch not in _DEB_ARCHITECTURES:
        raise ValueError(f"unsupported Debian architecture {arch!r}")
    return (
        "Package: kiro-gateway-tray\n"
        f"Version: {version}\n"
        "Section: net\n"
        "Priority: optional\n"
        f"Architecture: {arch}\n"
        "Maintainer: Kiro Gateway Tray <zhujunsan@users.noreply.github.com>\n"
        "Homepage: https://github.com/zhujunsan/kiro-gateway-deploy\n"
        "Description: Local tray app for the Kiro gateway\n"
        " Runs kiro-gateway and exposes it through a Cloudflare Tunnel.\n"
    )


def _write_unix_text(path: Path, text: str) -> None:
    """Write ``text`` with LF line endings, including on Windows checkouts."""
    path.write_text(text, encoding="utf-8", newline="\n")


def stage_linux_deb(
    root: Path,
    bundle: Path,
    *,
    version: str,
    arch: str,
    icon: Path | None,
) -> None:
    """Lay out a ``dpkg-deb`` tree for the PyInstaller onedir bundle.

    The frozen program stays in ``/opt/kiro-gateway-tray`` so its libraries
    remain beside the executable. ``/usr/bin/kiro-gateway-tray`` is a shell
    wrapper, and the desktop entry plus pixmap register it in the app menu.

    Args:
        root: Package root that will contain ``DEBIAN``, ``opt``, and ``usr``.
        bundle: PyInstaller ``dist/kiro-gateway-tray`` directory.
        version: Debian package version.
        arch: ``amd64`` or ``arm64``.
        icon: PNG installed as the menu icon, or None to ship without one.

    Raises:
        FileNotFoundError: ``bundle`` has no ``kiro-gateway-tray`` executable.
    """
    binary = bundle / _LINUX_BINARY
    if not binary.is_file():
        raise FileNotFoundError(
            f"PyInstaller bundle is missing {binary}; run pyinstaller before packaging"
        )

    opt = root / "opt" / "kiro-gateway-tray"
    if opt.exists():
        shutil.rmtree(opt)
    shutil.copytree(bundle, opt, symlinks=True)
    staged_bin = opt / _LINUX_BINARY
    staged_bin.chmod(staged_bin.stat().st_mode | 0o755)

    bindir = root / "usr" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    _write_unix_text(
        bindir / _LINUX_BINARY,
        "#!/bin/sh\n"
        'exec /opt/kiro-gateway-tray/kiro-gateway-tray "$@"\n',
    )
    (bindir / _LINUX_BINARY).chmod(0o755)

    applications = root / "usr" / "share" / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    _write_unix_text(
        applications / "kiro-gateway-tray.desktop",
        render_desktop_entry("/usr/bin/kiro-gateway-tray"),
    )

    pixmap = root / "usr" / "share" / "pixmaps" / "kiro-gateway-tray.png"
    if icon is not None and icon.is_file():
        pixmap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(icon, pixmap)
    elif pixmap.exists():
        pixmap.unlink()

    debian = root / "DEBIAN"
    debian.mkdir(parents=True, exist_ok=True)
    control = debian / "control"
    _write_unix_text(control, render_deb_control(version, arch))
    control.chmod(0o644)


def _assert_deb_paths_do_not_overlap(bundle: Path, stage_dir: Path, output: Path) -> None:
    """Reject a staging directory that would delete or recurse into the bundle."""
    bundle_res = bundle.resolve()
    stage_res = stage_dir.resolve()
    output_res = output.resolve()
    if (
        stage_res == bundle_res
        or bundle_res in stage_res.parents
        or stage_res in bundle_res.parents
    ):
        raise ValueError(
            f"deb stage directory {stage_dir} must not overlap the PyInstaller bundle {bundle}"
        )
    if output_res == stage_res or stage_res in output_res.parents:
        raise ValueError(
            f"deb output {output} must not be inside the stage directory {stage_dir}"
        )


def build_linux_deb(
    bundle: Path,
    output: Path,
    *,
    version: str,
    machine: str,
    icon: Path | None,
    stage_dir: Path,
) -> Path:
    """Build a ``.deb`` from the PyInstaller bundle for ``machine``.

    Args:
        bundle: PyInstaller ``dist/kiro-gateway-tray`` directory.
        output: Destination ``.deb`` path.
        version: Debian package version.
        machine: Value of ``platform.machine()``. Selects ``amd64`` or ``arm64``.
        icon: PNG installed as the menu icon, or None.
        stage_dir: Scratch directory. Replaced on every build.

    Returns:
        ``output``.

    Raises:
        ValueError: The stage directory overlaps the bundle or the output file.
        FileNotFoundError: ``dpkg-deb`` is not installed, or the bundle is incomplete.
    """
    _assert_deb_paths_do_not_overlap(bundle, stage_dir, output)
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    root = stage_dir / "kiro-gateway-tray"
    root.mkdir(parents=True)
    stage_linux_deb(
        root,
        bundle,
        version=version,
        arch=deb_arch(machine),
        icon=icon,
    )
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise FileNotFoundError(
            "dpkg-deb not found; install the dpkg package to build the .deb"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [dpkg_deb, "--root-owner-group", "--build", str(root), str(output)],
        check=True,
    )
    return output


def build_linux_appimage() -> Path:
    machine = platform.machine()
    out = OUT / linux_appimage_filename(VER, machine)
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

    # .desktop file (required by AppImage spec). Exec stays a bare name;
    # appimagetool rewrites it against the mounted AppDir.
    _write_unix_text(
        appdir / "kiro-gateway-tray.desktop",
        render_desktop_entry("kiro-gateway-tray"),
    )

    # Icon
    icon_src = APP / "resources" / "icon.png"
    if icon_src.exists():
        shutil.copy(icon_src, appdir / "kiro-gateway-tray.png")
        shutil.copy(icon_src, appdir / ".DirIcon")

    # appimagetool downloaded by CI to app/build/, one file per CPU architecture.
    appimagetool = build_dir / appimagetool_filename(machine)
    if not appimagetool.exists():
        raise FileNotFoundError(
            f"appimagetool not found at {appimagetool}; "
            "CI should have downloaded it via the fetch appimagetool step"
        )

    env = {**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1", "ARCH": appimage_arch(machine)}
    subprocess.run(
        [str(appimagetool), str(appdir), str(out)],
        check=True,
        env=env,
    )
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)

    if sys.platform == "darwin":
        outputs = [build_macos_dmg()]
    elif sys.platform.startswith("win"):
        outputs = [build_windows_installer()]
    else:
        machine = platform.machine()
        outputs = [
            build_linux_appimage(),
            build_linux_deb(
                DIST / "kiro-gateway-tray",
                OUT / linux_deb_filename(VER, machine),
                version=VER,
                machine=machine,
                icon=APP / "resources" / "icon.png",
                stage_dir=APP / "build" / "deb-stage",
            ),
        ]

    for out in outputs:
        print(f"[ok] {out.name}")


if __name__ == "__main__":
    main()
