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
# _strip removes debug/symbol tables from bundled binaries to shrink the app.
# Windows is excluded: PyInstaller's strip needs binutils' `strip`, which isn't
# present on the runner and can corrupt PE files. macOS strip is safe here
# because ad-hoc signing happens later in make_dist.py (after packaging).
if sys.platform.startswith("win"):
    _cf_sub, _cf_exe, _name = "windows-amd64", "cloudflared.exe", "KiroGatewayTray"
    _console = False
    _strip = False
elif sys.platform == "darwin":
    import platform as _pf
    _arch = "arm64" if _pf.machine() == "arm64" else "amd64"
    _cf_sub, _cf_exe, _name = f"darwin-{_arch}", "cloudflared", "KiroGatewayTray"
    _console = False
    _strip = True
else:
    _cf_sub, _cf_exe, _name = "linux-amd64", "cloudflared", "kiro-gateway-tray"
    _console = True
    _strip = True

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
            "fastapi", "pystray", "loguru", "dotenv", "starlette", "pydantic",
            "sentry_sdk"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

hiddenimports += collect_submodules("tiktoken_ext")
hiddenimports += collect_submodules("loguru")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("sentry_sdk")
hiddenimports += ["sqlite3", "_sqlite3"]
# httpx imports socksio lazily only when it sees a socks:// proxy, so PyInstaller
# can't detect it statically. Without this the frozen app crashes on a socks://
# HTTP(S)_PROXY / ALL_PROXY with "Unknown scheme for proxy URL".
hiddenimports += collect_submodules("socksio")
a = Analysis(
    [str(APP / "kiro_gateway_tray" / "__main__.py")],
    pathex=[str(APP), str(VENDOR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Build-time / dev-only modules that the frozen app never imports. Trimming
    # them shrinks the bundle without touching runtime behavior. tkinter is a
    # big one (Tk/Tcl); the app's tray UI is pystray, not tkinter.
    excludes=[
        "pytest", "hypothesis",
        "tkinter", "test", "unittest",
        "distutils", "setuptools", "pip", "pkg_resources",
        "pydoc", "doctest", "lib2to3",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=_name,
    console=_console,
    strip=_strip,
    icon=_bundle_icon,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=_strip, name=_name)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{_name}.app",
        icon=_bundle_icon,
        bundle_identifier="top.botsonny.kiro-gateway-tray",
        info_plist={
            "LSUIElement": True,            # menu-bar only; no Dock icon
            "NSHighResolutionCapable": True,
            # macOS 26 (Tahoe) Liquid Glass icon: rendered from the compiled
            # AppIcon.icon -> Assets.car catalog installed below. Older macOS
            # ignores this key and falls back to CFBundleIconFile (icon.icns),
            # which PyInstaller sets automatically from BUNDLE(icon=...).
            "CFBundleIconName": "AppIcon",
        },
    )

    # Compile resources/AppIcon.icon -> Assets.car (Xcode 26 actool) and place
    # it in Contents/Resources so macOS 26 renders the layered icon. Falls back
    # to the checked-in resources/Assets.car when actool is unavailable.
    import importlib.util as _ilu
    _mi = APP / "packaging" / "macos_icon.py"
    _spec = _ilu.spec_from_file_location("macos_icon", _mi)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _app_path = Path(DISTPATH) / f"{_name}.app"
    if _app_path.exists():
        _mod.install_into_app(_app_path)
