"""Kiro Gateway tray app."""

__version__ = "0.1.15"

# Upstream gateway this app vendors. We use a fork that already has the
# kiro-* model aliases and the /usage endpoint baked into source, so no
# build-time patching is needed. Pinned to a commit for reproducible builds.
UPSTREAM_SHA = "e974e1740be0cfd0c4d7f332ffff00462cc651dd"
UPSTREAM_REPO = "https://github.com/zhujunsan/kiro-gateway.git"

# This app's own repo, used by the update checker (Task 13) to query the
# latest GitHub release. Format: "owner/repo".
GITHUB_REPO = "zhujunsan/kiro-gateway-deploy"
