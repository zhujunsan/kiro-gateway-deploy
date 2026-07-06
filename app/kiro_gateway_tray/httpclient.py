"""Shared HTTP client helpers.

Two distinct needs:

* **Local probes** (health / usage / models) target 127.0.0.1. httpx (unlike
  requests) does NOT bypass localhost for HTTP(S)_PROXY, so a user/corp proxy
  would hijack these and make a healthy gateway look down. These use
  ``trust_env=False`` to ignore the environment proxy entirely.

* **Remote calls** (provision Worker, telemetry, update check) SHOULD honour the
  user's proxy — users behind a SOCKS proxy need these to go through it. But
  httpx only accepts ``http/https/socks5/socks5h`` schemes, while proxy clients
  often export the generic ``socks://``. We resolve + normalize the proxy
  ourselves (``socks://`` -> ``socks5h://``) and pass it explicitly, so a
  socks:// proxy doesn't crash client construction.
"""
from __future__ import annotations

import os

import httpx

# httpx precedence for an https:// target: scheme-specific proxy over ALL_PROXY.
_PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


def normalize_proxy_url(url: str | None) -> str | None:
    """Return an httpx-acceptable proxy URL, or None when nothing usable.

    ``socks://`` / ``socks4://`` are rewritten to ``socks5h://`` (DNS resolved
    proxy-side). Already-valid schemes pass through; a bare host:port is assumed
    to be http://.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    scheme, sep, rest = url.partition("://")
    if not sep:
        return f"http://{url}"
    if scheme.lower() in ("socks", "socks4"):
        return f"socks5h://{rest}"
    return url


def resolve_proxy() -> str | None:
    """Resolve the outbound proxy from the environment, normalized for httpx.

    Returns the first set proxy env var (httpx precedence order) normalized, or
    None when no proxy is configured.
    """
    for var in _PROXY_ENV_VARS:
        val = os.environ.get(var)
        if val:
            return normalize_proxy_url(val)
    return None


def local_client(*, timeout: float) -> httpx.Client:
    """A persistent httpx client for localhost probes.

    ``trust_env=False`` so a corp/system proxy can't intercept 127.0.0.1 calls.
    Caller owns the returned client's lifecycle (close it on teardown).
    """
    return httpx.Client(timeout=timeout, trust_env=False)
