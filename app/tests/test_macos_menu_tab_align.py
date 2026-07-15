# app/tests/test_macos_menu_tab_align.py
"""Shared trailing-tab alignment for macOS tray status tags."""
from __future__ import annotations

from kiro_gateway_tray.macos_menu import (
    _TAG_GAP,
    _pad_tab_right,
    shared_right_tab_pos,
)


def test_shared_right_tab_pos_uses_widest_row():
    # Narrow status (运行中) vs wider tag (✓ 已开启): tab stop must follow the wide row.
    short = (100.0, 36.0)  # gateway-ish left + 运行中
    wide = (80.0, 90.0)  # shorter left + wide trailing tag
    pos = shared_right_tab_pos([short, wide])
    assert pos == wide[0] + _TAG_GAP + wide[1]
    assert pos > short[0] + _TAG_GAP + short[1]


def test_shared_right_tab_pos_empty():
    assert shared_right_tab_pos([]) == 0.0


def test_pad_tab_right_idempotent_for_copy_badge():
    assert _pad_tab_right("复制") == "   复制   "
    assert _pad_tab_right("   复制   ") == "   复制   "
    assert _pad_tab_right("运行中") == "运行中"


def test_per_item_tab_would_leave_status_mid_row():
    """Regression: live-patch used left+gap+right per row →「运行中」sat mid-row.

    Shared tab_pos must equal the menu-wide max, not the short row's natural width.
    """
    rows = [
        (200.0, 40.0),  # 🖥 网关: 本地 Kiro Gateway + 运行中
        (210.0, 40.0),  # 🌐 隧道: Cloudflare Tunnel + 运行中
        (90.0, 55.0),  # 📡 进行中 + 空闲
        (70.0, 70.0),  # 🚀 开机自启 + ✓ 已开启
        (100.0, 60.0),  # 当前版本 + 已是最新
        (160.0, 50.0),  # URL row + 复制 badge width
    ]
    shared = shared_right_tab_pos(rows)
    gateway_natural = rows[0][0] + _TAG_GAP + rows[0][1]
    activity_natural = rows[2][0] + _TAG_GAP + rows[2][1]
    assert shared > gateway_natural
    assert shared > activity_natural
    assert shared == max(l + _TAG_GAP + r for l, r in rows)
