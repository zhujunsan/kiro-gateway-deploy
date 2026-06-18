"""Kiro Gateway tray app."""

__version__ = "0.1.13"

# Upstream gateway this app vendors. We use a fork that already has the
# kiro-* model aliases and the /usage endpoint baked into source, so no
# build-time patching is needed. Pinned to a commit for reproducible builds.
UPSTREAM_SHA = "a3683391cb9f2dd08ae1d3f174a83d278c4ca1dc"
UPSTREAM_REPO = "https://github.com/zhujunsan/kiro-gateway.git"

# This app's own repo, used by the update checker (Task 13) to query the
# latest GitHub release. Format: "owner/repo".
GITHUB_REPO = "zhujunsan/kiro-gateway-deploy"
