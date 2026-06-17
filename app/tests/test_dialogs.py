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
