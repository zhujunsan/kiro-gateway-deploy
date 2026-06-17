# app/tests/test_inject_metadata.py
from scripts.inject_metadata import inject

SAMPLE = '''\
"""Kiro Gateway tray app."""

__version__ = "0.1.0"

UPSTREAM_SHA = "deadbeef"
UPSTREAM_REPO = "https://github.com/old/repo.git"

GITHUB_REPO = "old/repo"
'''


def test_inject_repo_and_version():
    out = inject(SAMPLE, {"GITHUB_REPOSITORY": "owner/name", "GITHUB_REF_NAME": "v1.2.3"})
    assert 'GITHUB_REPO = "owner/name"' in out
    assert '__version__ = "1.2.3"' in out


def test_non_tag_ref_leaves_version_untouched():
    out = inject(SAMPLE, {"GITHUB_REPOSITORY": "owner/name", "GITHUB_REF_NAME": "main"})
    assert '__version__ = "0.1.0"' in out
    assert 'GITHUB_REPO = "owner/name"' in out


def test_upstream_overrides_optional():
    out = inject(
        SAMPLE,
        {
            "UPSTREAM_REPO_OVERRIDE": "https://github.com/new/up.git",
            "UPSTREAM_SHA_OVERRIDE": "cafef00d",
        },
    )
    assert 'UPSTREAM_REPO = "https://github.com/new/up.git"' in out
    assert 'UPSTREAM_SHA = "cafef00d"' in out


def test_empty_env_is_noop():
    assert inject(SAMPLE, {}) == SAMPLE
