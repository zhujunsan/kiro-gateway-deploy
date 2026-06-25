# app/tests/test_dialogs.py
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
