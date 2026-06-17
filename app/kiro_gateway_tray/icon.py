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


def make_icon(running: bool):
    """Return the template negative-space silhouette (transparent + opaque black
    body). The system tints it automatically."""
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
