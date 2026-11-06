"""Release architecture tokens for Linux x86_64 and ARM64 artifacts."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "packaging"))
from scripts.fetch_cloudflared import release_asset
from target_arch import (  # noqa: E402
    appimage_arch,
    appimagetool_filename,
    cloudflared_arch,
    linux_appimage_filename,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "amd64"),
        ("amd64", "amd64"),
        ("AMD64", "amd64"),
        ("arm64", "arm64"),
        ("aarch64", "arm64"),
        ("AARCH64", "arm64"),
    ],
)
def test_cloudflared_arch(machine: str, expected: str) -> None:
    assert cloudflared_arch(machine) == expected


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("arm64", "aarch64"),
        ("aarch64", "aarch64"),
    ],
)
def test_appimage_arch(machine: str, expected: str) -> None:
    assert appimage_arch(machine) == expected


def test_linux_arm_artifact_names() -> None:
    assert linux_appimage_filename("1.2.3", "aarch64") == (
        "kiro-gateway-tray-1.2.3-linux-aarch64.AppImage"
    )
    assert appimagetool_filename("aarch64") == "appimagetool-aarch64.AppImage"
    assert linux_appimage_filename("1.2.3", "x86_64") == (
        "kiro-gateway-tray-1.2.3-linux-x86_64.AppImage"
    )


def test_unsupported_machine_is_rejected() -> None:
    with pytest.raises(ValueError, match="riscv64"):
        cloudflared_arch("riscv64")
    with pytest.raises(ValueError, match="riscv64"):
        appimage_arch("riscv64")


def test_cloudflared_release_assets() -> None:
    assert release_asset("windows", "amd64") == "cloudflared-windows-amd64.exe"
    assert release_asset("linux", "arm64") == "cloudflared-linux-arm64"
    assert release_asset("linux", "amd64") == "cloudflared-linux-amd64"
    with pytest.raises(ValueError, match="riscv64"):
        release_asset("linux", "riscv64")


def test_spec_bundles_cloudflared_for_the_build_machine() -> None:
    spec = (ROOT / "app" / "packaging" / "kiro_gateway_tray.spec").read_text(encoding="utf-8")
    assert "cloudflared_arch" in spec
    assert 'f"linux-{_arch}"' in spec
    assert '"windows-amd64"' in spec
    assert '"linux-amd64"' not in spec


def test_make_dist_names_appimage_from_machine() -> None:
    source = (ROOT / "app" / "packaging" / "make_dist.py").read_text(encoding="utf-8")
    assert "linux_appimage_filename" in source
    assert "appimagetool_filename" in source
    assert "ARCH" in source
    assert "appimagetool-x86_64.AppImage" not in source


def test_release_workflow_builds_native_linux_arm() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-app.yml").read_text(encoding="utf-8")
    assert "- os: ubuntu-24.04-arm" in workflow
    arm = re.search(
        r"- os: ubuntu-24\.04-arm\n\s+arch: arm64",
        workflow,
    )
    assert arm is not None
    assert "tool_arch=aarch64" in workflow
    assert "appimagetool-${tool_arch}.AppImage" in workflow
    assert "appimagetool-x86_64.AppImage" not in workflow


def test_release_workflow_has_no_windows_arm_runner() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-app.yml").read_text(encoding="utf-8")
    assert "windows-11-arm" not in workflow
    assert "windows-arm64" not in workflow


def test_release_uploads_installers_without_sha256_sidecars() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-app.yml").read_text(encoding="utf-8")
    dist = (ROOT / "app" / "packaging" / "make_dist.py").read_text(encoding="utf-8")
    assert "!app/release/*.sha256" in workflow
    assert ".dmg.sha256" not in workflow
    assert "sha256sum" in workflow
    assert ".sha256" not in dist
