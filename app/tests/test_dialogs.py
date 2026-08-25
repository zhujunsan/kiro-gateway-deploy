# app/tests/test_dialogs.py
import sys
import types

from kiro_gateway_tray import dialogs


def test_escape_applescript_backslash_then_quote():
    # backslash must be doubled first, then quotes escaped
    assert dialogs.escape_applescript(r"a\b") == r"a\\b"
    assert dialogs.escape_applescript('say "hi"') == 'say \\"hi\\"'
    # a path with both: the backslash is escaped, the quote is escaped
    assert dialogs.escape_applescript(r'C:\x"y') == r'C:\\x\"y'


def test_escape_applescript_idempotent_on_plain_text():
    assert dialogs.escape_applescript("https://example.com/v1") == "https://example.com/v1"


def test_generate_api_key_length_and_charset():
    key = dialogs.generate_api_key(40)
    assert len(key) == 40
    assert key.isalnum()
    # two calls should not collide
    assert dialogs.generate_api_key() != dialogs.generate_api_key()


def test_validate_url():
    assert dialogs.validate_url("https://example.com") is None
    assert dialogs.validate_url("http://kg.example.com/path") is None
    assert dialogs.validate_url("  https://example.com  ") is None  # whitespace tolerated
    assert dialogs.validate_url("") is not None
    assert dialogs.validate_url("example.com") is not None
    assert dialogs.validate_url("ftp://example.com") is not None


def test_validate_secret():
    assert dialogs.validate_secret("abc123") is None
    assert dialogs.validate_secret("   ") is not None
    assert dialogs.validate_secret("") is not None


def test_validate_profile_arn():
    good = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCdef123"
    assert dialogs.validate_profile_arn(good) is None
    assert dialogs.validate_profile_arn(f"  {good}  ") is None  # whitespace tolerated
    # wrong service / shape
    assert dialogs.validate_profile_arn("") is not None
    assert dialogs.validate_profile_arn("arn:aws:iam::123456789012:user/foo") is not None
    # bad account id (not 12 digits) and embedded newline
    assert dialogs.validate_profile_arn("arn:aws:codewhisperer:us-east-1:123:profile/X") is not None
    assert dialogs.validate_profile_arn(good.replace(":profile", "\n:profile")) is not None


def test_prompt_validated_reprompts_until_valid(monkeypatch):
    answers = iter(["bad", "also-bad", "https://ok.example.com"])
    seen_prompts = []

    def fake_prompt_input(title, prompt, default="", hidden=False, multiline=False):
        seen_prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(dialogs, "prompt_input", fake_prompt_input)
    result = dialogs.prompt_validated(
        "t", "请输入地址", validate=dialogs.validate_url,
    )
    assert result == "https://ok.example.com"
    # third call succeeded; first two prompts retried with an error appended
    assert len(seen_prompts) == 3
    assert "⚠️" in seen_prompts[1]


def test_prompt_validated_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(
        dialogs, "prompt_input",
        lambda *a, **k: "still-bad",
    )
    try:
        dialogs.prompt_validated(
            "t", "p", validate=dialogs.validate_url, max_attempts=3,
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "校验失败" in str(e)


def test_alert_dispatches_to_darwin(monkeypatch):
    called = []
    monkeypatch.setattr(dialogs.sys, "platform", "darwin")
    monkeypatch.setattr(dialogs, "_darwin_alert", lambda t, m: called.append((t, m)))
    dialogs.alert("标题", "内容")
    assert called == [("标题", "内容")]


def test_alert_dispatches_to_win32(monkeypatch):
    called = []
    monkeypatch.setattr(dialogs.sys, "platform", "win32")
    monkeypatch.setattr(dialogs, "_win32_alert", lambda t, m: called.append((t, m)))
    dialogs.alert("标题", "内容")
    assert called == [("标题", "内容")]


def test_alert_swallows_backend_errors(monkeypatch):
    monkeypatch.setattr(dialogs.sys, "platform", "darwin")

    def _boom(t, m):
        raise OSError("display failed")

    monkeypatch.setattr(dialogs, "_darwin_alert", _boom)
    dialogs.alert("标题", "内容")  # must not raise


def test_linux_alert_uses_zenity_when_present(monkeypatch):
    cmds = []
    monkeypatch.setattr(dialogs.shutil, "which", lambda name: name if name == "zenity" else None)
    monkeypatch.setattr(
        dialogs.subprocess, "run",
        lambda cmd, **k: cmds.append(cmd),
    )
    dialogs._linux_alert("T", "M")
    assert cmds and cmds[0][0] == "zenity"
    assert any(str(a).startswith("--window-icon=") for a in cmds[0])


def test_linux_alert_uses_kdialog_icon(monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    cmds = []
    monkeypatch.setattr(dialogs, "_alert_icon_path", lambda *n: icon)
    monkeypatch.setattr(dialogs.shutil, "which", lambda name: name if name == "kdialog" else None)
    monkeypatch.setattr(dialogs.subprocess, "run", lambda cmd, **k: cmds.append(cmd))
    dialogs._linux_alert("T", "M")
    assert cmds[0][:4] == ["kdialog", "--icon", str(icon), "--title"]


def test_osascript_alert_includes_app_icon(monkeypatch, tmp_path):
    icon = tmp_path / "icon.icns"
    icon.write_bytes(b"icns")
    cmds = []
    monkeypatch.setattr(dialogs.subprocess, "run", lambda cmd, **k: cmds.append(cmd))
    dialogs._osascript_alert("标题", "第一行\n第二行", icon)
    script = cmds[0][cmds[0].index("-e") + 1]
    assert "display dialog" in script
    assert "POSIX file" in script
    assert str(icon.resolve()) in script
    assert "确定" in script


def test_darwin_alert_falls_back_to_osascript(monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    monkeypatch.setattr(dialogs, "_alert_icon_path", lambda *n: icon)
    monkeypatch.setattr(
        dialogs, "_cocoa_alert",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no AppKit")),
    )
    seen = []
    monkeypatch.setattr(dialogs, "_osascript_alert", lambda t, m, p: seen.append((t, m, p)))
    dialogs._darwin_alert("T", "M")
    assert seen == [("T", "M", icon)]


def test_win32_alert_embeds_png_icon(monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    monkeypatch.setattr(dialogs, "_alert_icon_path", lambda *n: icon)
    scripts = []

    def _run(cmd, **k):
        scripts.append(cmd[cmd.index("-Command") + 1])

    monkeypatch.setattr(dialogs.subprocess, "run", _run)
    dialogs._win32_alert("T", "hello")
    assert "FromFile" in scripts[0]
    assert str(icon) in scripts[0]


def test_cocoa_alert_sets_icon(monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    created = []

    class _Image:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithContentsOfFile_(self, path):
            self.path = path
            return self

    class _Alert:
        @classmethod
        def alloc(cls):
            inst = cls.__new__(cls)
            created.append(inst)
            return inst

        def init(self):
            return self

        def setMessageText_(self, title):
            self.title = title

        def setInformativeText_(self, message):
            self.message = message

        def addButtonWithTitle_(self, label):
            self.button = label

        def setIcon_(self, image):
            self.icon = image

        def runModal(self):
            self.ran = True
            return 1000

    class _NSApp:
        @staticmethod
        def sharedApplication():
            return object()

    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        types.SimpleNamespace(NSAlert=_Alert, NSApplication=_NSApp, NSImage=_Image),
    )
    monkeypatch.setattr(dialogs, "_run_cocoa_modal", lambda fn: fn())
    dialogs._cocoa_alert("隧道地址已变更", "请改 Base URL", icon)
    assert len(created) == 1
    alert = created[0]
    assert alert.title == "隧道地址已变更"
    assert alert.icon.path == str(icon)
    assert alert.ran is True


def test_run_cocoa_modal_runs_inline_on_main_thread(monkeypatch):
    class _Thread:
        @staticmethod
        def isMainThread():
            return True

    monkeypatch.setitem(sys.modules, "Foundation", types.SimpleNamespace(NSThread=_Thread))
    ran = []
    dialogs._run_cocoa_modal(lambda: ran.append(1))
    assert ran == [1]


def test_app_icon_file_finds_bundled_png():
    from kiro_gateway_tray.icon import app_icon_file

    path = app_icon_file("icon.png")
    assert path is not None
    assert path.name == "icon.png"
    assert path.exists()


def test_app_icon_file_returns_none_for_missing():
    from kiro_gateway_tray.icon import app_icon_file

    assert app_icon_file("definitely-missing-icon.xyz") is None

