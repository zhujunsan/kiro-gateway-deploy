"""Debian package layout built beside the Linux AppImage."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "packaging"))
import make_dist  # noqa: E402


def _bundle(tmp_path: Path, *, mode: int = 0o644) -> Path:
    bundle = tmp_path / "kiro-gateway-tray"
    nested = bundle / "internal"
    nested.mkdir(parents=True)
    binary = bundle / "kiro-gateway-tray"
    binary.write_text("#!/bin/sh\necho app\n", encoding="utf-8")
    binary.chmod(mode)
    (nested / "lib.so").write_text("lib", encoding="utf-8")
    return bundle


def _stage(tmp_path: Path, bundle: Path, **kwargs: object) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    icon = kwargs.pop("icon", None)
    make_dist.stage_linux_deb(
        root,
        bundle,
        version=str(kwargs.pop("version", "1.2.3")),
        arch=str(kwargs.pop("arch", "amd64")),
        icon=icon if isinstance(icon, Path) or icon is None else None,
    )
    return root


def test_stage_installs_bundle_wrapper_desktop_and_control(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG\r\n")
    root = _stage(tmp_path, bundle, icon=icon)

    staged = (root / "opt" / "kiro-gateway-tray" / "kiro-gateway-tray").read_text(
        encoding="utf-8"
    )
    assert staged.startswith("#!/bin/sh")
    assert (root / "opt" / "kiro-gateway-tray" / "internal" / "lib.so").read_text(
        encoding="utf-8"
    ) == "lib"
    wrapper = root / "usr" / "bin" / "kiro-gateway-tray"
    assert wrapper.read_bytes() == (
        b"#!/bin/sh\nexec /opt/kiro-gateway-tray/kiro-gateway-tray \"$@\"\n"
    )
    desktop = (root / "usr" / "share" / "applications" / "kiro-gateway-tray.desktop").read_text(
        encoding="utf-8"
    )
    assert "Exec=/usr/bin/kiro-gateway-tray\n" in desktop
    assert "Terminal=false\n" in desktop
    assert (root / "usr" / "share" / "pixmaps" / "kiro-gateway-tray.png").read_bytes() == b"\x89PNG\r\n"
    control = (root / "DEBIAN" / "control").read_text(encoding="utf-8")
    assert control == (
        "Package: kiro-gateway-tray\n"
        "Version: 1.2.3\n"
        "Section: net\n"
        "Priority: optional\n"
        "Architecture: amd64\n"
        "Maintainer: Kiro Gateway Tray <zhujunsan@users.noreply.github.com>\n"
        "Homepage: https://github.com/zhujunsan/kiro-gateway-deploy\n"
        "Description: Local tray app for the Kiro gateway\n"
        " Runs kiro-gateway and exposes it through a Cloudflare Tunnel.\n"
    )
    if os.name != "nt":
        assert wrapper.stat().st_mode & stat.S_IXUSR
        assert (root / "opt" / "kiro-gateway-tray" / "kiro-gateway-tray").stat().st_mode & stat.S_IXUSR
        assert (root / "DEBIAN" / "control").stat().st_mode & stat.S_IXUSR == 0


def test_stage_keeps_symlinks_inside_the_bundle(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink layout is exercised on the Linux release runners")
    bundle = _bundle(tmp_path)
    (bundle / "lib.so").symlink_to("internal/lib.so")
    (bundle / "outside").symlink_to("/etc/passwd")
    root = _stage(tmp_path, bundle)
    opt = root / "opt" / "kiro-gateway-tray"
    assert (opt / "lib.so").is_symlink()
    assert os.readlink(opt / "lib.so") == "internal/lib.so"
    assert (opt / "outside").is_symlink()
    assert os.readlink(opt / "outside") == "/etc/passwd"


def test_stage_without_icon_removes_a_stale_pixmap(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    root = _stage(tmp_path, bundle, icon=icon)
    pixmap = root / "usr" / "share" / "pixmaps" / "kiro-gateway-tray.png"
    assert pixmap.is_file()
    make_dist.stage_linux_deb(root, bundle, version="1.2.3", arch="arm64", icon=None)
    assert not pixmap.exists()
    assert "Architecture: arm64\n" in (root / "DEBIAN" / "control").read_text(encoding="utf-8")


def test_stage_rejects_a_bundle_without_the_executable(tmp_path: Path) -> None:
    bundle = tmp_path / "empty"
    bundle.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(FileNotFoundError, match="kiro-gateway-tray"):
        make_dist.stage_linux_deb(root, bundle, version="1.2.3", arch="amd64", icon=None)
    assert not (root / "DEBIAN").exists()


@pytest.mark.parametrize(
    "version",
    ["", "v1.2.3", "1.2.3_1", "1.2.3\nArchitecture: all", "1.2.3 "],
)
def test_control_rejects_versions_dpkg_would_misread(version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        make_dist.render_deb_control(version, "amd64")


@pytest.mark.parametrize("arch", ["", "x86_64", "aarch64", "AMD64", "all"])
def test_control_rejects_non_debian_architectures(arch: str) -> None:
    with pytest.raises(ValueError, match="architecture"):
        make_dist.render_deb_control("1.2.3", arch)


def test_desktop_exec_rejects_newlines() -> None:
    with pytest.raises(ValueError, match="Exec"):
        make_dist.render_desktop_entry("kiro\nHidden=true")
    assert "Exec=kiro-gateway-tray\n" in make_dist.render_desktop_entry("kiro-gateway-tray")


def test_build_rejects_overlapping_paths(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    output = tmp_path / "out.deb"
    with pytest.raises(ValueError, match="overlap"):
        make_dist.build_linux_deb(
            bundle,
            output,
            version="1.2.3",
            machine="x86_64",
            icon=None,
            stage_dir=bundle / "deb-stage",
        )
    with pytest.raises(ValueError, match="overlap"):
        make_dist.build_linux_deb(
            bundle,
            output,
            version="1.2.3",
            machine="x86_64",
            icon=None,
            stage_dir=tmp_path,
        )
    with pytest.raises(ValueError, match="stage directory"):
        make_dist.build_linux_deb(
            bundle,
            tmp_path / "stage" / "pkg.deb",
            version="1.2.3",
            machine="x86_64",
            icon=None,
            stage_dir=tmp_path / "stage",
        )
    assert not output.exists()


def test_build_reports_a_missing_dpkg_deb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(make_dist.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="dpkg-deb"):
        make_dist.build_linux_deb(
            bundle,
            tmp_path / "release" / "kiro-gateway-tray_1.2.3_amd64.deb",
            version="1.2.3",
            machine="x86_64",
            icon=None,
            stage_dir=tmp_path / "deb-stage",
        )


@pytest.mark.skipif(make_dist.shutil.which("dpkg-deb") is None, reason="dpkg-deb builds the release .deb")
def test_dpkg_deb_packages_root_owned_amd64_and_arm64(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, mode=0o755)
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    for machine, arch in (("x86_64", "amd64"), ("aarch64", "arm64")):
        output = tmp_path / "release" / f"kiro-gateway-tray_1.2.3_{arch}.deb"
        make_dist.build_linux_deb(
            bundle,
            output,
            version="1.2.3",
            machine=machine,
            icon=icon,
            stage_dir=tmp_path / f"stage-{arch}",
        )
        info = subprocess.check_output(["dpkg-deb", "--info", str(output)], text=True)
        listing = subprocess.check_output(["dpkg-deb", "--contents", str(output)], text=True)
        assert f"Architecture: {arch}" in info
        assert "Version: 1.2.3" in info
        assert "./opt/kiro-gateway-tray/kiro-gateway-tray" in listing
        assert "./usr/bin/kiro-gateway-tray" in listing
        assert "./usr/share/applications/kiro-gateway-tray.desktop" in listing
        assert "./usr/share/pixmaps/kiro-gateway-tray.png" in listing
        assert "root/root" in listing
