# app/kiro_gateway_tray/icon.py
"""Menu-bar icon rendering.

The source icon-source.png is a "black rounded square + white k→ glyph"; the
black body (square minus glyph) is exactly the alpha shape we want — body
opaque, glyph and corners transparent. With macOS template mode on, the system
tints it: white in dark menu bars, inverted in light bars, auto-adapting.

Status is encoded by SHAPE (template drops color): bottom-right corner is a
checkmark when running, a cross when stopped. Packaged (PyInstaller) assets come
from sys._MEIPASS; from source they sit in app/resources/.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Draw on a high-res canvas and let Cocoa scale to menu-bar size; combined with
# the Retina fix this stays crisp.
_TRAY_RENDER = 256
# Transparent padding ratio: native menu-bar icons all have breathing room.
# Content occupies 75% -> padding 12.5%.
_TRAY_PAD = 0.125

_SILHOUETTE = None
_SILHOUETTE_LOADED = False


def _asset_path(name: str) -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for cand in (Path(meipass) / "resources" / name, Path(meipass) / name):
            if cand.exists():
                return cand
    return Path(__file__).resolve().parent.parent / "resources" / name


def _load_silhouette():
    """Extract the negative-space silhouette alpha mask from icon-source.png.

    Black body -> opaque, white glyph / outside-corners -> transparent. i.e.
    alpha = inverted grayscale, cropped to the body bbox.
    """
    from PIL import Image

    src = _asset_path("icon-source.png")
    if not src.exists():
        src = _asset_path("icon.png")
    if not src.exists():
        return None
    try:
        gray = Image.open(src).convert("L")
    except Exception:
        return None
    alpha = gray.point(lambda p: 255 - p)  # black(0)->255 opaque; white(255)->0 transparent
    bbox = alpha.point(lambda p: 255 if p > 32 else 0).getbbox()
    if bbox:
        alpha = alpha.crop(bbox)
    return alpha


def _silhouette():
    global _SILHOUETTE, _SILHOUETTE_LOADED
    if not _SILHOUETTE_LOADED:
        _SILHOUETTE = _load_silhouette()
        _SILHOUETTE_LOADED = True
    return _SILHOUETTE


def windows_uses_light_theme() -> bool:
    """Return True if the Windows taskbar is using the LIGHT theme.

    Reads ``SystemUsesLightTheme`` (DWORD) under
    ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize``:
    1 = light taskbar, 0 = dark taskbar. Returns False (assume dark) on any
    error, missing key, or non-Windows platform — dark is the safe default since
    a missing value means we render the light-bodied icon, which is the historic
    behavior.

    ``winreg`` is stdlib but Windows-only, so it is imported lazily inside the
    win32 guard to keep this module importable on macOS/Linux.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            return bool(value)
    except Exception:
        return False


def _is_template_platform() -> bool:
    """Only macOS menu bars auto-tint "template" icons.

    pystray on macOS sets the NSImage as a template image, so the system
    recolors the opaque-body / knocked-out-glyph silhouette to match the menu
    bar (white in dark mode, black in light mode). On Windows (and other
    platforms) pystray has NO template-tinting mechanism — it blits the raw
    RGBA image onto the taskbar. A macOS-style icon (opaque black body with the
    glyph knocked out to alpha=0) then renders as "black on black" on a dark
    taskbar and is unreadable. So those platforms need a self-colored icon.
    """
    return sys.platform == "darwin"


def _make_icon_template(running: bool):
    """macOS template icon: opaque black body, glyph + status shape knocked out
    to alpha=0 so the system tint shows the background through them."""
    from PIL import Image, ImageChops, ImageDraw

    size = _TRAY_RENDER
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    pad = int(size * _TRAY_PAD)
    box = size - 2 * pad
    sil = _silhouette()
    if sil is not None:
        alpha = sil.resize((box, box), Image.LANCZOS)
        body = Image.new("RGBA", (box, box), (0, 0, 0, 255))
        body.putalpha(alpha)
        canvas.paste(body, (pad, pad), body)
    else:
        ImageDraw.Draw(canvas).rounded_rectangle(
            (pad, pad, pad + box - 1, pad + box - 1),
            radius=int(box * 0.22),
            fill=(0, 0, 0, 255),
        )

    # Status shape in the bottom-right corner, knocked out of the silhouette
    # (alpha=0) so the tinted body shows the background through it.
    # running = checkmark ✓; stopped = cross ✗. Shape-coded because template
    # mode keeps no color.
    overlay = Image.new("L", (size, size), 0)
    od = ImageDraw.Draw(overlay)
    r = int(size * 0.24)
    x1 = size - pad
    y1 = size - pad
    cx, cy = x1 - r // 2, y1 - r // 2
    sw = max(3, int(size * 0.06))  # stroke width
    if running:
        # checkmark: short leg down-right, long leg up-right
        od.line([(cx - r // 3, cy), (cx - r // 8, cy + r // 3)], fill=255, width=sw)
        od.line([(cx - r // 8, cy + r // 3), (cx + r // 3, cy - r // 4)], fill=255, width=sw)
    else:
        # cross: two diagonal lines
        half = r // 3
        od.line([(cx - half, cy - half), (cx + half, cy + half)], fill=255, width=sw)
        od.line([(cx - half, cy + half), (cx + half, cy - half)], fill=255, width=sw)
    ca = canvas.getchannel("A")
    knocked = ImageChops.subtract(ca, overlay)
    canvas.putalpha(knocked)
    return canvas


def _make_icon_solid(running: bool, light_theme: bool = False):
    """Self-colored icon for Windows / non-template platforms.

    pystray does not template-tint icons off macOS, so we cannot rely on the
    system recoloring a knocked-out silhouette. Instead we render a fully
    self-contained image carrying its own contrast.

    Color inversion follows the taskbar theme (the icon must contrast AGAINST
    the bar it sits on):
      - DARK taskbar  (light_theme=False): WHITE body + BLACK glyph + BLACK
        check/cross — a light icon stands out on a dark bar.
      - LIGHT taskbar (light_theme=True):  BLACK body + WHITE glyph + WHITE
        check/cross — a dark icon stands out on a light bar.
    """
    from PIL import Image, ImageDraw

    # Pick body vs. foreground (glyph + status strokes) colors so the icon
    # always contrasts with the taskbar. On a light taskbar we invert to a dark
    # body with light foreground; on a dark taskbar we keep the light body with
    # dark foreground.
    if light_theme:
        body_rgb = (30, 30, 30)
        fg_rgb = (255, 255, 255, 255)
    else:
        body_rgb = (245, 245, 245)
        fg_rgb = (0, 0, 0, 255)

    size = _TRAY_RENDER
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    pad = int(size * _TRAY_PAD)
    box = size - 2 * pad
    radius = int(box * 0.22)

    # Rounded-square body mask (opaque inside the rounded rect, transparent at
    # the corners) used both as the body shape and to constrain the glyph.
    body_mask = Image.new("L", (box, box), 0)
    ImageDraw.Draw(body_mask).rounded_rectangle(
        (0, 0, box - 1, box - 1), radius=radius, fill=255
    )

    body = Image.new("RGBA", (box, box), (*body_rgb, 255))
    body.putalpha(body_mask)

    sil = _silhouette()
    if sil is not None:
        # Silhouette alpha: body=255 opaque, glyph/corners=0 transparent. The
        # glyph is where alpha is LOW *inside* the rounded square, so paint
        # those pixels with the foreground color on top of the body.
        alpha = sil.resize((box, box), Image.LANCZOS)
        glyph_fg = Image.new("RGBA", (box, box), fg_rgb)
        glyph_alpha = Image.new("L", (box, box), 0)
        a_px = alpha.load()
        m_px = body_mask.load()
        g_px = glyph_alpha.load()
        for y in range(box):
            for x in range(box):
                # inside the rounded square AND in the glyph (transparent) area
                if m_px[x, y] > 127 and a_px[x, y] < 128:
                    g_px[x, y] = 255
        glyph_fg.putalpha(glyph_alpha)
        body = Image.alpha_composite(body, glyph_fg)
    else:
        # Fallback: no silhouette asset -> draw a simple "k" placeholder glyph
        # centered on the body, in the foreground color.
        gd = ImageDraw.Draw(body)
        gw = max(3, int(box * 0.08))
        # vertical stem
        gd.line([(int(box * 0.34), int(box * 0.22)), (int(box * 0.34), int(box * 0.78))], fill=fg_rgb, width=gw)
        # upper diagonal
        gd.line([(int(box * 0.66), int(box * 0.40)), (int(box * 0.34), int(box * 0.58))], fill=fg_rgb, width=gw)
        # lower diagonal
        gd.line([(int(box * 0.34), int(box * 0.58)), (int(box * 0.66), int(box * 0.78))], fill=fg_rgb, width=gw)

    canvas.paste(body, (pad, pad), body)

    # Status shape in the bottom-right corner, drawn as opaque foreground-color
    # strokes (not knocked out) so it is visible on the body. Same geometry as
    # the macOS path. running = checkmark ✓; stopped = cross ✗.
    od = ImageDraw.Draw(canvas)
    r = int(size * 0.24)
    x1 = size - pad
    y1 = size - pad
    cx, cy = x1 - r // 2, y1 - r // 2
    sw = max(3, int(size * 0.06))  # stroke width
    if running:
        od.line([(cx - r // 3, cy), (cx - r // 8, cy + r // 3)], fill=fg_rgb, width=sw)
        od.line([(cx - r // 8, cy + r // 3), (cx + r // 3, cy - r // 4)], fill=fg_rgb, width=sw)
    else:
        half = r // 3
        od.line([(cx - half, cy - half), (cx + half, cy + half)], fill=fg_rgb, width=sw)
        od.line([(cx - half, cy + half), (cx + half, cy - half)], fill=fg_rgb, width=sw)
    return canvas


def make_icon(running: bool, light_theme: bool | None = None):
    """Return the tray icon image for the current platform.

    macOS gets the template negative-space silhouette (the system tints it, so
    ``light_theme`` is ignored). Windows / other platforms get a self-colored
    icon whose colors invert to contrast with the taskbar theme.

    ``light_theme`` controls the Windows coloring:
      - None  -> auto-detect via ``windows_uses_light_theme()`` (default; keeps
        existing ``make_icon(False)`` / ``make_icon(True)`` callers working).
      - True  -> render the dark-bodied icon for a LIGHT taskbar.
      - False -> render the light-bodied icon for a DARK taskbar.
    """
    if _is_template_platform():
        return _make_icon_template(running)
    if light_theme is None:
        light_theme = windows_uses_light_theme()
    return _make_icon_solid(running, light_theme=light_theme)
