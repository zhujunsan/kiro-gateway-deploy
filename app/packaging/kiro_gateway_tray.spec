# app/packaging/kiro_gateway_tray.spec
# Run from app/ dir: pyinstaller packaging/kiro_gateway_tray.spec
# Prereq (CI does this): python scripts/vendor_sync.py && python scripts/fetch_cloudflared.py
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

APP = Path(SPECPATH).resolve().parent          # app/
VENDOR = APP / "kiro_gateway_tray" / "vendor"
RES = APP / "resources"

# --- platform → cloudflared subdir + exe name ---
if sys.platform.startswith("win"):
    _cf_sub, _cf_exe, _name = "windows-amd64", "cloudflared.exe", "KiroGatewayTray"
    _console = False
elif sys.platform == "darwin":
    import platform as _pf
    _arch = "arm64" if _pf.machine() == "arm64" else "amd64"
    _cf_sub, _cf_exe, _name = f"darwin-{_arch}", "cloudflared", "KiroGatewayTray"
    _console = False
else:
    _cf_sub, _cf_exe, _name = "linux-amd64", "cloudflared", "kiro-gateway-tray"
    _console = True

datas = [
    (str(VENDOR), "vendor"),
    (str(RES / "cloudflared" / _cf_sub / _cf_exe), f"resources/cloudflared/{_cf_sub}"),
]
# Bundle the menu-bar glyph source + fallback PNG so tray.make_icon() finds them
# at runtime (looked up via sys._MEIPASS/resources). Only include what exists.
for _res in ("icon-source.png", "icon.png"):
    _rp = RES / _res
    if _rp.exists():
        datas.append((str(_rp), "resources"))

# macOS .app bundle icon (Finder/Dock); not used for the menu-bar glyph.
_icns = RES / "icon.icns"
_bundle_icon = str(_icns) if _icns.exists() else None

binaries = []
hiddenimports = []

for pkg in ("tiktoken", "tiktoken_ext", "uvicorn", "websockets", "httptools",
            "fastapi", "pystray", "loguru", "dotenv", "starlette", "pydantic"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

hiddenimports += collect_submodules("tiktoken_ext")
hiddenimports += collect_submodules("loguru")
hiddenimports += collect_submodules("starlette")
hiddenimports += ["sqlite3", "_sqlite3"]

a = Analysis(
    [str(APP / "kiro_gateway_tray" / "__main__.py")],
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
    icon=_bundle_icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name=_name)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{_name}.app",
        icon=_bundle_icon,
        bundle_identifier="net.zhujunsan.kiro-gateway-tray",
        info_plist={
            "LSUIElement": True,            # menu-bar only; no Dock icon
            "NSHighResolutionCapable": True,
        },
    )
