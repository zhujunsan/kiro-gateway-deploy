# app/scripts/vendor_sync.py
"""Clone the (already-patched) upstream fork at the pinned SHA and copy the
files the app embeds. The fork bakes in the required model aliases and /usage,
so there is no build-time patching step."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiro_gateway_tray import UPSTREAM_REPO, UPSTREAM_SHA  # noqa: E402

VENDOR = ROOT / "kiro_gateway_tray" / "vendor"
COPY_ITEMS = ["main.py", "kiro", "requirements.txt"]


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> None:
    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    VENDOR.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _run(["git", "clone", "--no-checkout", UPSTREAM_REPO, "src"], cwd=tmp_path)
        src = tmp_path / "src"
        _run(["git", "checkout", UPSTREAM_SHA], cwd=src)
        for item in COPY_ITEMS:
            s = src / item
            d = VENDOR / item
            if s.is_dir():
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)

    (VENDOR / "__init__.py").write_text("")
    print(f"[ok] vendored fork {UPSTREAM_SHA[:7]} into {VENDOR}")


if __name__ == "__main__":
    main()
