"""Kiro Gateway tray app."""

__version__ = "0.1.0"

# Upstream jwadow/kiro-gateway commit this app vendors and patches against.
UPSTREAM_SHA = "a5292ca"
UPSTREAM_REPO = "https://github.com/jwadow/kiro-gateway.git"

# This app's own repo, used by the update checker (Task 13) to query the
# latest GitHub release. Format: "owner/repo".
GITHUB_REPO = "zhujunsan/kiro-gateway-deploy"
