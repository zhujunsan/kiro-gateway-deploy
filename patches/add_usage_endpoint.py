"""Patch kiro-gateway to expose a GET /usage endpoint.

Upstream kiro-gateway never calls Amazon Q's hidden `getUsageLimits` API, so it
can't report account quota. kiro.rs (hank9999/kiro.rs) does: it hits
`https://q.{region}.amazonaws.com/getUsageLimits` with the SSO access token and
parses subscription tier + usage. We replicate that here as a single appended
FastAPI route.

Design choices (verified against the live API on 2026-06-10):
- Reuse the gateway's own auth_manager (app.state.account_manager) so we get an
  auto-refreshed access token and never fight the gateway's refresh logic / steal
  its refresh_token. Reading the raw token file directly would risk using a stale
  token after the gateway refreshes in-memory.
- region comes from the profileArn (its 4th ARN segment), NOT auth_manager.q_host
  (which is runtime.{region}.kiro.dev) and NOT the token's top-level region (that's
  the auth region). The quota API lives on q.{region}.amazonaws.com.
- machineId in the User-Agent is cosmetic; the endpoint does not validate it. We
  pass auth_manager.fingerprint for consistency with what the gateway already sends.
- Auth on /usage reuses the existing PROXY_API_KEY check (verify_api_key).

Idempotent: guarded by a sentinel, appended (not block-rewritten) so it survives
most upstream formatting changes. main.py is started via uvicorn.run("main:app"),
which re-imports the module, so code appended at file end executes on import and
registers the route.
"""
from pathlib import Path

MAIN = Path("/app/main.py")
SENTINEL = "# >>> kiro-gateway usage endpoint >>>"

ENDPOINT_CODE = '''

# >>> kiro-gateway usage endpoint >>>
from fastapi import Request as _UsageRequest, Depends as _UsageDepends, HTTPException as _UsageHTTPException
from kiro.routes_openai import verify_api_key as _usage_verify_api_key

_USAGE_KIRO_VERSION = "0.11.107"
_USAGE_NODE_VERSION = "22.22.0"
_USAGE_SYSTEM_VERSION = "darwin#24.6.0"


def _usage_pick_auth():
    """Return an initialized auth_manager from the gateway's account pool, if any."""
    am = getattr(app.state, "account_manager", None)
    if am is None:
        return None, am
    for _acc in am._accounts.values():
        if _acc.auth_manager is not None:
            return _acc.auth_manager, am
    return None, am


def _usage_summary(data: dict) -> dict:
    """Replicate kiro.rs usage_limits aggregation: base + active trial + active bonuses."""
    sub = (data.get("subscriptionInfo") or {}).get("subscriptionTitle")
    breakdowns = data.get("usageBreakdownList") or []
    if not breakdowns:
        return {"subscription": sub, "nextDateReset": data.get("nextDateReset"), "breakdowns": []}

    out = []
    for b in breakdowns:
        used = b.get("currentUsageWithPrecision", b.get("currentUsage", 0)) or 0
        limit = b.get("usageLimitWithPrecision", b.get("usageLimit", 0)) or 0
        trial = b.get("freeTrialInfo")
        if trial and trial.get("freeTrialStatus") == "ACTIVE":
            used += trial.get("currentUsageWithPrecision", 0) or 0
            limit += trial.get("usageLimitWithPrecision", 0) or 0
        for bonus in b.get("bonuses") or []:
            if bonus.get("status") == "ACTIVE":
                used += bonus.get("currentUsage", 0) or 0
                limit += bonus.get("usageLimit", 0) or 0
        out.append({
            "used": round(used, 2),
            "limit": round(limit, 2),
        })
    return {"subscription": sub, "nextDateReset": data.get("nextDateReset"), "breakdowns": out}


@app.get("/usage", dependencies=[_UsageDepends(_usage_verify_api_key)])
async def _kiro_usage(request: _UsageRequest, raw: bool = False):
    """Account quota via Amazon Q getUsageLimits. Auth: Bearer PROXY_API_KEY."""
    import urllib.parse
    import httpx

    auth, am = _usage_pick_auth()
    if auth is None:
        if am is not None:
            _ids = list(am._accounts.keys())
            if _ids:
                try:
                    await am._initialize_account(_ids[0])
                except Exception:
                    pass
                _acc = am._accounts.get(_ids[0])
                auth = _acc.auth_manager if _acc else None
    if auth is None:
        raise _UsageHTTPException(status_code=503, detail="No initialized Kiro account available")

    token = await auth.get_access_token()
    profile_arn = auth.profile_arn

    region = None
    if profile_arn:
        _parts = profile_arn.split(":")
        if len(_parts) > 3 and _parts[3]:
            region = _parts[3]
    region = region or auth.region or "us-east-1"

    host = "q.{}.amazonaws.com".format(region)
    url = "https://{}/getUsageLimits?origin=AI_EDITOR&resourceType=AGENTIC_REQUEST".format(host)
    if profile_arn:
        url += "&profileArn=" + urllib.parse.quote(profile_arn, safe="")

    mid = auth.fingerprint
    user_agent = (
        "aws-sdk-js/1.0.0 ua/2.1 os/{os} lang/js md/nodejs#{node} "
        "api/codewhispererruntime#1.0.0 m/N,E KiroIDE-{ver}-{mid}"
    ).format(os=_USAGE_SYSTEM_VERSION, node=_USAGE_NODE_VERSION, ver=_USAGE_KIRO_VERSION, mid=mid)
    amz_user_agent = "aws-sdk-js/1.0.0 KiroIDE-{ver}-{mid}".format(ver=_USAGE_KIRO_VERSION, mid=mid)

    headers = {
        "x-amz-user-agent": amz_user_agent,
        "user-agent": user_agent,
        "amz-sdk-invocation-id": str(__import__("uuid").uuid4()),
        "amz-sdk-request": "attempt=1; max=1",
        "Authorization": "Bearer {}".format(token),
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise _UsageHTTPException(status_code=502, detail="Upstream request failed: {}".format(e))

    if resp.status_code != 200:
        raise _UsageHTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    if raw:
        return data
    result = _usage_summary(data)
    result["region"] = region
    return result
# <<< kiro-gateway usage endpoint <<<
'''


def patch_main() -> None:
    src = MAIN.read_text()
    if SENTINEL in src:
        print("[skip] main.py /usage already patched")
        return
    if "app.include_router" not in src:
        import sys
        sys.exit("main.py: app.include_router not found (unexpected structure)")
    MAIN.write_text(src.rstrip() + "\n" + ENDPOINT_CODE)
    print("[ok] patched /usage endpoint")


if __name__ == "__main__":
    patch_main()
