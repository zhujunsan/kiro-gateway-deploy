"""Native input dialogs and alerts, cross-platform.

macOS prefers a native Cocoa ``NSAlert`` with an accessory view so long values
(e.g. a profileArn) can be shown in a word-wrapping, multi-line field instead of
a one-line box that scrolls out of view. It falls back to ``osascript`` (plain
``display dialog``, single-line). Windows uses WinForms via PowerShell, and
Linux uses zenity (primary) or kdialog (secondary). Kept separate from tray.py
so the dialog flows can be reasoned about and tested in isolation.
"""
from __future__ import annotations

import re
import secrets
import shutil
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
    """Windows: WinForms 对话框（支持多行和密码掩码），无需第三方依赖。"""
    escaped_title = title.replace('"', '`"')
    escaped_prompt = prompt.replace('"', '`"').replace("\n", "`n")
    escaped_default = default.replace('"', '`"').replace("\n", "`n")

    # 用 ClientSize（内容区，不含标题栏和边框）来驱动布局，控件按紧凑的垂直间距
    # 排布，避免底部出现多余留白。
    margin = 15
    width = 400
    label_y = margin
    label_h = 40
    textbox_y = label_y + label_h + 5
    textbox_height = 120 if multiline else 23
    buttons_y = textbox_y + textbox_height + 12
    client_height = buttons_y + 28 + margin
    textbox_multiline = "$true" if multiline else "$false"
    textbox_scrollbars = '"Vertical"' if multiline else '"None"'
    textbox_password = "$true" if hidden else "$false"

    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$form = New-Object System.Windows.Forms.Form\n"
        f'$form.Text = "{escaped_title}"\n'
        f"$form.ClientSize = New-Object System.Drawing.Size({width},{client_height})\n"
        "$form.StartPosition = 'CenterScreen'\n"
        "$form.TopMost = $true\n"
        "$form.FormBorderStyle = 'FixedDialog'\n"
        "$form.MaximizeBox = $false\n"
        "$form.MinimizeBox = $false\n"
        "$label = New-Object System.Windows.Forms.Label\n"
        f"$label.Location = New-Object System.Drawing.Point({margin},{label_y})\n"
        f"$label.Size = New-Object System.Drawing.Size({width - 2 * margin},{label_h})\n"
        f'$label.Text = "{escaped_prompt}"\n'
        "$label.AutoSize = $false\n"
        f"$label.MaximumSize = New-Object System.Drawing.Size({width - 2 * margin},0)\n"
        "$label.AutoEllipsis = $false\n"
        "$form.Controls.Add($label)\n"
        "$tb = New-Object System.Windows.Forms.TextBox\n"
        f"$tb.Location = New-Object System.Drawing.Point({margin},{textbox_y})\n"
        f"$tb.Size = New-Object System.Drawing.Size({width - 2 * margin},{textbox_height})\n"
        f"$tb.Multiline = {textbox_multiline}\n"
        f"$tb.ScrollBars = {textbox_scrollbars}\n"
        f"$tb.UseSystemPasswordChar = {textbox_password}\n"
        f'$tb.Text = "{escaped_default}"\n'
        "$form.Controls.Add($tb)\n"
        "$okBtn = New-Object System.Windows.Forms.Button\n"
        "$okBtn.Text = 'OK'\n"
        f"$okBtn.Location = New-Object System.Drawing.Point({width - 2 * 80 - margin + 10},{buttons_y})\n"
        "$okBtn.Size = New-Object System.Drawing.Size(75,28)\n"
        "$okBtn.DialogResult = [System.Windows.Forms.DialogResult]::OK\n"
        "$form.Controls.Add($okBtn)\n"
        "$cancelBtn = New-Object System.Windows.Forms.Button\n"
        "$cancelBtn.Text = 'Cancel'\n"
        f"$cancelBtn.Location = New-Object System.Drawing.Point({width - 80 - margin + 5},{buttons_y})\n"
        "$cancelBtn.Size = New-Object System.Drawing.Size(75,28)\n"
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


def _linux_input(
    title: str,
    prompt: str,
    default: str = "",
    hidden: bool = False,
    multiline: bool = False,
) -> str:
    """Linux input via zenity (primary) or kdialog (secondary).

    Ubuntu 24.04+ defaults to GNOME, so zenity is preferred. ``multiline`` is a
    nice-to-have here; the values we prompt for (e.g. a profileArn) are single
    line, so we keep it pragmatic and use a single-line entry on both tools.
    """
    if shutil.which("zenity"):
        cmd = ["zenity", "--entry", f"--title={title}", f"--text={prompt}"]
        if hidden:
            # --hide-text can't combine with a default entry-text; that's fine.
            cmd.append("--hide-text")
        elif default:
            cmd.append(f"--entry-text={default}")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
        except Exception as e:
            raise RuntimeError(f"无法弹出输入框: {e}")
        if result.returncode != 0:
            raise RuntimeError("用户取消了操作。")
        return result.stdout.strip()

    if shutil.which("kdialog"):
        if hidden:
            cmd = ["kdialog", "--title", title, "--password", prompt]
        else:
            cmd = ["kdialog", "--title", title, "--inputbox", prompt, default]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
        except Exception as e:
            raise RuntimeError(f"无法弹出输入框: {e}")
        if result.returncode != 0:
            raise RuntimeError("用户取消了操作。")
        return result.stdout.strip()

    raise RuntimeError(
        "未检测到图形对话框工具（zenity/kdialog），"
        "请改用 CLI 模式（kiro-gateway-tray --cli）。"
    )


def prompt_input(
    title: str,
    prompt: str,
    default: str = "",
    hidden: bool = False,
    multiline: bool = False,
) -> str:
    """Cross-platform input prompt.

    macOS uses native Cocoa (falling back to AppleScript), Windows uses WinForms
    via PowerShell, and Linux uses zenity or kdialog. ``multiline`` (macOS only)
    shows a word-wrapping multi-line field, useful for long values like a
    profileArn that would otherwise scroll out of view.
    """
    if sys.platform == "darwin":
        try:
            return _cocoa_input(title, prompt, default, hidden=hidden, multiline=multiline)
        except RuntimeError:
            raise
        except Exception:
            # AppKit unavailable (rare); fall back to the AppleScript dialog.
            return _osascript_input(title, prompt, default, hidden)
    if sys.platform == "win32":
        return _win32_powershell_input(title, prompt, default, hidden, multiline)
    # Linux / other Unix: zenity → kdialog → error
    return _linux_input(title, prompt, default, hidden=hidden, multiline=multiline)


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

