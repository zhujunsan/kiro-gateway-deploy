# app/tests/test_bump_cask.py
import pytest

from scripts.bump_cask import bump

ARM = "1" * 64
INTEL = "2" * 64
SAMPLE = '''\
cask "kiro-gateway-tray" do
  version "0.1.0"

  on_arm do
    sha256 "aaaa"
    url "https://example.com/v#{version}/x-arm64.dmg"
  end
  on_intel do
    sha256 "bbbb"
    url "https://example.com/v#{version}/x-amd64.dmg"
  end

  app "KiroGatewayTray.app"
end
'''


def test_bump_replaces_version_and_both_shas():
    out = bump(SAMPLE, "0.2.0", ARM, INTEL)
    assert 'version "0.2.0"' in out
    assert f'sha256 "{ARM}"' in out
    assert f'sha256 "{INTEL}"' in out
    assert "0.1.0" not in out


def test_bump_strips_leading_v():
    out = bump(SAMPLE, "v1.2.3", ARM, INTEL)
    assert 'version "1.2.3"' in out


def test_bump_assigns_sha_to_correct_block():
    out = bump(SAMPLE, "0.2.0", ARM, INTEL)
    arm_block = out.split("on_intel")[0]
    intel_block = out.split("on_intel")[1]
    assert ARM in arm_block and INTEL not in arm_block
    assert INTEL in intel_block and ARM not in intel_block


def test_bump_rejects_bad_sha():
    with pytest.raises(ValueError):
        bump(SAMPLE, "0.2.0", "nothex", INTEL)


def test_bump_requires_version_anchor():
    with pytest.raises(ValueError):
        bump('cask "x" do\nend\n', "0.2.0", ARM, INTEL)
