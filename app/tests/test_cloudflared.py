# app/tests/test_cloudflared.py
from pathlib import Path
from kiro_tray import cloudflared, appconfig


def test_binary_name_per_platform():
    import sys
    name = cloudflared._binary_name()
    if sys.platform.startswith("win"):
        assert name == "cloudflared.exe"
    else:
        assert name == "cloudflared"


def test_binary_path_missing_raises(monkeypatch):
    monkeypatch.setattr(cloudflared, "_candidate_dirs", lambda: [Path("/no/such")])
    try:
        cloudflared.binary_path()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "cloudflared" in str(e).lower()


def test_provision_email_to_username():
    from kiro_tray.provision import _email_to_username
    assert _email_to_username("alice@example.com") == "alice"
    assert _email_to_username("john.doe@example.com") == "john-doe"
    assert _email_to_username("ALICE@example.com") == "alice"
