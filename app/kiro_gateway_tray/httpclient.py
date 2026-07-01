"""Shared HTTP client factory for localhost probes.

The tray's health / usage / models probes all target 127.0.0.1, but httpx
(unlike requests) does NOT bypass localhost for HTTP(S)_PROXY env vars. A
system/corp proxy without 127.0.0.1 in NO_PROXY would otherwise hijack every
probe and make a healthy gateway/tunnel look unreachable. Building the client
here means that ``trust_env=False`` rationale lives in exactly one place instead
of being copy-pasted (and drifting) across modules.
"""
from __future__ import annotations

import httpx


def local_client(*, timeout: float) -> httpx.Client:
    """A persistent httpx client for localhost probes.

    ``trust_env=False`` so a corp/system proxy can't intercept 127.0.0.1 calls.
    Caller owns the returned client's lifecycle (close it on teardown).
    """
    return httpx.Client(timeout=timeout, trust_env=False)
