"""Cognito authentication — verify JWTs issued by our Cognito User Pool.

Design (matches the deploy):
  - Auth is ON only when DEPLOY_PROFILE=aws (the cloud). Locally it's a no-op, so
    `make run` on your Mac never asks you to log in.
  - The ALB stays plain HTTP; THIS app verifies the token itself. That's the
    portable pattern — the same JWT-verification logic works in any framework.

How verification works:
  A Cognito login gives the browser a signed JWT. We fetch Cognito's PUBLIC keys
  (the JWKS) once, cache them, and check the token's signature + claims (issuer,
  expiry, audience). If it's valid we trust the `sub`/`email` inside; if not, 401.
  We never see or store passwords — Cognito owns that.
"""
from __future__ import annotations

import time
from functools import lru_cache

from fastapi import Depends, HTTPException, Header

from app.config import get_settings

_s = get_settings()

# ---- is auth active this run? ------------------------------------------------
def _auth_on() -> bool:
    # only enforce in the cloud, and only if a pool is actually configured
    return _s.deploy_profile == "aws" and bool(getattr(_s, "cognito_user_pool_id", ""))


# ---- JWKS (Cognito's public signing keys), fetched once and cached -----------
@lru_cache(maxsize=1)
def _jwks() -> dict:
    import urllib.request
    import json

    region = _s.aws_region
    pool = _s.cognito_user_pool_id
    url = f"https://cognito-idp.{region}.amazonaws.com/{pool}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def _issuer() -> str:
    return f"https://cognito-idp.{_s.aws_region}.amazonaws.com/{_s.cognito_user_pool_id}"


def _verify(token: str) -> dict:
    """Verify signature + claims, return the token's payload (or raise 401)."""
    try:
        from jose import jwt  # python-jose handles the RS256 + JWKS dance
    except Exception:
        # library missing — fail closed rather than letting anyone in
        raise HTTPException(500, "auth library not installed")

    try:
        headers = jwt.get_unverified_header(token)
        kid = headers["kid"]
        key = next((k for k in _jwks()["keys"] if k["kid"] == kid), None)
        if key is None:
            # keys rotate rarely; refresh once before giving up
            _jwks.cache_clear()
            key = next((k for k in _jwks()["keys"] if k["kid"] == kid), None)
        if key is None:
            raise HTTPException(401, "unknown signing key")

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=_issuer(),
            audience=_s.cognito_client_id,
            options={"verify_at_hash": False},
        )
        if claims.get("exp", 0) < time.time():
            raise HTTPException(401, "token expired")
        return claims
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"invalid token: {type(e).__name__}")


# ---- the dependency endpoints use -------------------------------------------
def current_user(authorization: str | None = Header(default=None)) -> str:
    """Return the verified user id. In local dev (auth off) returns a dev user.

    Usage in an endpoint:
        def plan(req: PlanRequest, user_id: str = Depends(current_user)):
    """
    if not _auth_on():
        return "local-dev"  # local runs are unauthenticated by design

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    claims = _verify(token)
    # prefer a stable subject; fall back to email
    return claims.get("sub") or claims.get("email") or "unknown"
