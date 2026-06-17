# app/packaging/kiro_tray.spec
# Run from app/ dir: pyinstaller packaging/kiro_tray.spec
# Prereq (CI does this): python scripts/vendor_sync.py && python scripts/fetch_cloudflared.py
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

APP = Path(SPECPATH).resolve().parent          # app/
VENDOR = APP / "kiro_tray" / "vendor"
RES = APP / "resources"

# --- platform → cloudflared subdir + exe name ---
if sys.platform.startswith("win"):
    _cf_sub, _cf_exe, _name = "windows-amd64", "cloudflared.exe", "KiroTray"
    _console = False
elif sys.platform == "darwin":
    import platform as _pf
    _arch = "arm64" if _pf.machine() == "arm64" else "amd64"
    _cf_sub, _cf_exe, _name = f"darwin-{_arch}", "cloudflared", "KiroTray"
    _console = False
else:
    _cf_sub, _cf_exe, _name = "linux-amd64", "cloudflared", "kiro-tray"
    _console = True

datas = [
    (str(VENDOR), "vendor"),
    (str(RES / "cloudflared" / _cf_sub / _cf_exe), f"resources/cloudflared/{_cf_sub}"),
    (str(RES / "icon.png"), "resources"),
]
binaries = []
hiddenimports = []

for pkg in ("tiktoken", "tiktoken_ext", "uvicorn", "websockets", "httptools", "fastapi", "pystray"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

hiddenimports += collect_submodules("tiktoken_ext")

a = Analysis(
    [str(APP / "kiro_tray" / "__main__.py")],
    pathex=[str(APP), str(VENDOR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "hypothesis"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=_name,
    console=_console,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=_name)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{_name}.app",
        icon=None,
        bundle_identifier="dev.kiro.tray",
        info_plist={"LSUIElement": True},
    )
