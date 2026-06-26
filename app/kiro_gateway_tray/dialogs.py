"""Native input dialogs and alerts, cross-platform.

macOS prefers a native Cocoa ``NSAlert`` with an accessory view so long values
(e.g. a profileArn) can be shown in a word-wrapping, multi-line field instead of
a one-line box that scrolls out of view. It falls back to ``osascript`` (plain
``display dialog``, single-line) and then tkinter. Kept separate from tray.py so
the dialog flows can be reasoned about and tested in isolation.
"""
from __future__ import annotations

import re
import secrets
import string
import subprocess
import sys
from typing import Callable


def escape_applescript(s: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal.
    Backslashes must be escaped first, then double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def generate_api_key(length: int = 32) -> str:
    """Generate a cryptographically random alphanumeric API key."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- input validators -------------------------------------------------------
# Each returns an error message (str) on bad input, or None when the value is
# acceptable. They are deliberately lenient about surrounding whitespace; the
# caller strips the value before persisting it.

_ARN_RE = re.compile(
    r"^arn:aws:codewhisperer:[a-z]{2}-[a-z]+-\d+:\d{12}:profile/[A-Za-z0-9]+$"
)


def validate_url(value: str) -> str | None:
    v = value.strip()
    if not v:
        return "地址不能为空。"
    if not re.match(r"^https?://[^\s/]+", v):
        return "地址格式不正确，应以 http:// 或 https:// 开头，例如\nhttps://kiro-gateway-provision.example.com"
    return None


def validate_secret(value: str) -> str | None:
    v = value.strip()
    if not v:
        return "激活码不能为空。"
    return None


def validate_profile_arn(value: str) -> str | None:
    v = value.strip()
    if not v:
        return "profileArn 不能为空。"
    if not _ARN_RE.match(v):
        return (
            "profileArn 格式不正确。应形如：\n"
            "arn:aws:codewhisperer:us-east-1:123456789012:profile/XXXX\n"
            "请检查是否有换行、空格或缺失片段。"
        )
    return None


def _win32_powershell_input(
    title: str,
    prompt: str,
    default: str = "",
    hidden: bool = False,
    multiline: bool = False,
) -> str:
    """Windows fallback: WinForms 对话框（支持多行和密码掩码），无需 tkinter。"""
    escaped_title = title.replace('"', '`"')
    escaped_prompt = prompt.replace('"', '`"').replace("\n", "`n")
    escaped_default = default.replace('"', '`"').replace("\n", "`n")

    form_height = "300" if multiline else "180"
    textbox_height = "80" if multiline else "20"
    textbox_multiline = "$true" if multiline else "$false"
    textbox_scrollbars = '"Vertical"' if multiline else '"None"'
    textbox_password = "$true" if hidden else "$false"

    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$form = New-Object System.Windows.Forms.Form\n"
        f'$form.Text = "{escaped_title}"\n'
        f"$form.Size = New-Object System.Drawing.Size(420,{form_height})\n"
        "$form.StartPosition = 'CenterScreen'\n"
        "$form.TopMost = $true\n"
        "$form.FormBorderStyle = 'FixedDialog'\n"
        "$form.MaximizeBox = $false\n"
        "$form.MinimizeBox = $false\n"
        "$label = New-Object System.Windows.Forms.Label\n"
        "$label.Location = New-Object System.Drawing.Point(10,10)\n"
        "$label.Size = New-Object System.Drawing.Size(380,50)\n"
        f'$label.Text = "{escaped_prompt}"\n'
        "$label.AutoSize = $false\n"
        "$label.MaximumSize = New-Object System.Drawing.Size(380,0)\n"
        "$label.AutoEllipsis = $false\n"
        "$form.Controls.Add($label)\n"
        "$tb = New-Object System.Windows.Forms.TextBox\n"
        "$tb.Location = New-Object System.Drawing.Point(10,65)\n"
        f"$tb.Size = New-Object System.Drawing.Size(380,{textbox_height})\n"
        f"$tb.Multiline = {textbox_multiline}\n"
        f"$tb.ScrollBars = {textbox_scrollbars}\n"
        f"$tb.UseSystemPasswordChar = {textbox_password}\n"
        f'$tb.Text = "{escaped_default}"\n'
        "$form.Controls.Add($tb)\n"
        "$okBtn = New-Object System.Windows.Forms.Button\n"
        "$okBtn.Text = 'OK'\n"
        f"$okBtn.Location = New-Object System.Drawing.Point(220,{int(form_height) - 70})\n"
        "$okBtn.Size = New-Object System.Drawing.Size(75,30)\n"
        "$okBtn.DialogResult = [System.Windows.Forms.DialogResult]::OK\n"
        "$form.Controls.Add($okBtn)\n"
        "$cancelBtn = New-Object System.Windows.Forms.Button\n"
        "$cancelBtn.Text = 'Cancel'\n"
        f"$cancelBtn.Location = New-Object System.Drawing.Point(310,{int(form_height) - 70})\n"
        "$cancelBtn.Size = New-Object System.Drawing.Size(75,30)\n"
        "$cancelBtn.DialogResult = [System.Windows.Forms.DialogResult]::Cancel\n"
        "$form.Controls.Add($cancelBtn)\n"
        "$form.AcceptButton = $okBtn\n"
        "$form.CancelButton = $cancelBtn\n"
        "$form.Add_Shown({$tb.Select()})\n"
        "$result = $form.ShowDialog()\n"
        'if ($result -eq [System.Windows.Forms.DialogResult]::OK) {\n'
        "  Write-Output $tb.Text\n"
        "  exit 0\n"
        "} else {\n"
        "  exit 1\n"
        "}\n"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=300,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except Exception as e:
        raise RuntimeError(f"无法弹出输入框: {e}")
    if result.returncode != 0:
        raise RuntimeError("用户取消了操作。")
    return result.stdout.strip()


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


def _cocoa_input(
    title: str,
    prompt: str,
    default: str = "",
    hidden: bool = False,
    multiline: bool = False,
    ok_label: str = "确定",
    cancel_label: str = "取消",
) -> str:
    """Native macOS input via NSAlert + accessory view.

    Uses a word-wrapping multi-line NSTextView (inside a scroll view) when
    ``multiline`` is set, so long values like a profileArn are fully visible
    and verifiable. Falls back to a single-line secure/plain field otherwise.
    Must be called on the main thread (AppKit requirement); first-run setup
    already runs there.
    """
    from AppKit import (
        NSAlert,
        NSApplication,
        NSMakeRect,
        NSScrollView,
        NSSecureTextField,
        NSTextField,
        NSTextView,
    )

    NSApplication.sharedApplication()

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(prompt)
    alert.addButtonWithTitle_(ok_label)
    alert.addButtonWithTitle_(cancel_label)

    width = 380.0
    if multiline:
        height = 96.0
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)  # NSBezelBorder
        text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        text_view.setString_(default or "")
        text_view.setRichText_(False)
        text_view.setFont_(NSTextField.labelWithString_("").font())
        text_view.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(text_view)
        alert.setAccessoryView_(scroll)
        accessory = scroll
        getter = lambda: text_view.string()
    else:
        height = 24.0
        cls = NSSecureTextField if hidden else NSTextField
        field = cls.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        field.setStringValue_(default or "")
        alert.setAccessoryView_(field)
        accessory = field
        getter = lambda: field.stringValue()

    alert.window().setInitialFirstResponder_(accessory)
    response = alert.runModal()
    # NSAlertFirstButtonReturn == 1000 (the OK button we added first)
    if response != 1000:
        raise RuntimeError("用户取消了操作。")
    return str(getter())


def prompt_input(
    title: str,
    prompt: str,
    default: str = "",
    hidden: bool = False,
    multiline: bool = False,
) -> str:
    """Cross-platform input prompt: native Cocoa on macOS, tkinter elsewhere.

    ``multiline`` (macOS only) shows a word-wrapping multi-line field, useful
    for long values like a profileArn that would otherwise scroll out of view.
    """
    if sys.platform == "darwin":
        try:
            return _cocoa_input(title, prompt, default, hidden=hidden, multiline=multiline)
        except RuntimeError:
            raise
        except Exception:
            # AppKit unavailable (rare); fall back to the AppleScript dialog.
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
        if sys.platform == "win32":
            return _win32_powershell_input(title, prompt, default, hidden, multiline)
        raise RuntimeError("无法弹出输入框，请改用 CLI 模式（kiro-gateway-tray --cli）。")


def prompt_validated(
    title: str,
    prompt: str,
    validate: Callable[[str], str | None],
    default: str = "",
    hidden: bool = False,
    multiline: bool = False,
    max_attempts: int = 5,
) -> str:
    """Prompt repeatedly until ``validate`` accepts the (stripped) input.

    ``validate`` returns an error message on bad input, or None when accepted.
    On a bad value the error is appended to the prompt and the dialog re-opens
    with the user's previous (raw) input pre-filled so they can fix it in place.
    Cancelling propagates the RuntimeError from prompt_input.
    """
    err: str | None = None
    current = default
    for _ in range(max_attempts):
        full_prompt = prompt if err is None else f"{prompt}\n\n⚠️ {err}"
        raw = prompt_input(title, full_prompt, default=current, hidden=hidden, multiline=multiline)
        current = raw
        err = validate(raw)
        if err is None:
            return raw.strip()
    raise RuntimeError(f"输入校验失败次数过多，已取消。\n{err or ''}".strip())


def alert(title: str, message: str) -> None:
    """Show a simple alert dialog (macOS only; no-op elsewhere)."""
    if sys.platform != "darwin":
        return
    escaped = escape_applescript(message).replace("\n", "\\n")
    subprocess.run(
        ["osascript", "-e", f'display alert "{escape_applescript(title)}" message "{escaped}"'],
        capture_output=True, timeout=30,
    )

