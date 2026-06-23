"""Kiro Gateway tray app."""

__version__ = "0.1.16"

# Upstream gateway this app vendors. We use a fork that already has the
# kiro-* model aliases and the /usage endpoint baked into source, so no
# build-time patching is needed. Pinned to a commit for reproducible builds.
UPSTREAM_SHA = "67a1a948a702b4d4ee47508e01612af60e05298c"
UPSTREAM_REPO = "https://github.com/zhujunsan/kiro-gateway.git"

# This app's own repo, used by the update checker (Task 13) to query the
# latest GitHub release. Format: "owner/repo".
GITHUB_REPO = "zhujunsan/kiro-gateway-deploy"
