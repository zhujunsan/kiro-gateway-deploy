"""Native input dialogs and alerts, cross-platform.

macOS uses ``osascript`` (plain ``display dialog``, no System Events permission
prompt); other platforms fall back to tkinter. Kept separate from tray.py so
the dialog flows can be reasoned about and tested in isolation.
"""
from __future__ import annotations

import secrets
import string
import subprocess
import sys


def escape_applescript(s: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal.
    Backslashes must be escaped first, then double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def generate_api_key(length: int = 32) -> str:
    """Generate a cryptographically random alphanumeric API key."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _osascript_input(title: str, prompt: str, default: str = "", hidden: bool = False) -> str:
    hidden_clause = "with hidden answer" if hidden else ""
    script = (
        f'display dialog "{escape_applescript(prompt)}" '
        f'default answer "{escape_applescript(default)}" '
        f'with title "{escape_applescript(title)}" '
        f'{hidden_clause}'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        raise RuntimeError(f"无法弹出对话框: {e}")
    if result.returncode != 0:
        raise RuntimeError("用户取消了操作。")
    for part in result.stdout.strip().split(", "):
        if part.startswith("text returned:"):
            return part[len("text returned:"):]
    raise RuntimeError("无法解析对话框返回值。")


def prompt_input(title: str, prompt: str, default: str = "", hidden: bool = False) -> str:
    """Cross-platform input prompt: osascript on macOS, tkinter elsewhere."""
    if sys.platform == "darwin":
        return _osascript_input(title, prompt, default, hidden)
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        val = simpledialog.askstring(title, prompt, parent=root, show="*" if hidden else None)
        root.destroy()
        if val is None:
            raise RuntimeError("用户取消了操作。")
        return val
    except ImportError:
        raise RuntimeError("无法弹出输入框，请改用 CLI 模式（kiro-gateway-tray --cli）。")


def alert(title: str, message: str) -> None:
    """Show a simple alert dialog (macOS only; no-op elsewhere)."""
    if sys.platform != "darwin":
        return
    escaped = escape_applescript(message).replace("\n", "\\n")
    subprocess.run(
        ["osascript", "-e", f'display alert "{escape_applescript(title)}" message "{escaped}"'],
        capture_output=True, timeout=30,
    )


def osascript_form_cf(title: str, default_url: str = "") -> tuple[str, str]:
    """macOS two-step form: provision URL then shared secret.

    Uses plain ``display dialog`` (no System Events, no permission prompt).
    Returns (provision_url, secret).
    """
    script = (
        f'display dialog "请输入 Provision 服务地址：\\n\\n由管理员提供的隧道签发服务 URL" '
        f'with title "{escape_applescript(title)} (1/2)" '
        f'default answer "{escape_applescript(default_url)}" '
        f'buttons {{"取消", "下一步"}} default button "下一步"'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError("用户取消了操作。")
    url = ""
    for part in result.stdout.strip().split(", "):
        if part.startswith("text returned:"):
            url = part[len("text returned:"):]
    if not url:
        raise RuntimeError("Provision 服务地址不能为空。")

    script2 = (
        f'display dialog "Worker: {escape_applescript(url)}\\n\\n请输入激活码（共享密钥）：" '
        f'with title "{escape_applescript(title)} (2/2)" '
        f'default answer "" '
        f'with hidden answer '
        f'buttons {{"上一步", "完成"}} default button "完成"'
    )
    result2 = subprocess.run(
        ["osascript", "-e", script2], capture_output=True, text=True, timeout=300,
    )
    if result2.returncode != 0:
        raise RuntimeError("用户取消了操作。")
    output = result2.stdout.strip()
    btn = ""
    secret = ""
    for part in output.split(", "):
        if part.startswith("button returned:"):
            btn = part[len("button returned:"):]
        elif part.startswith("text returned:"):
            secret = part[len("text returned:"):]
    if btn == "上一步":
        return osascript_form_cf(title, url)
    if not secret:
        raise RuntimeError("激活码不能为空。")
    return url, secret
