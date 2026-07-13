"""HTTP Basic auth, checked against env-configured credentials.

Single preassigned username/password for the whole tool — there is exactly one
reviewer, and this endpoint is only reachable at all because it's proxied through
Cloudflare + Nginx Proxy Manager over HTTPS, so Basic auth's plaintext-over-the-wire
weakness doesn't apply.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    expected_user = os.environ.get("CELLCOUNTS_USER")
    expected_pass = os.environ.get("CELLCOUNTS_PASS")
    if not expected_user or not expected_pass:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server is missing CELLCOUNTS_USER/CELLCOUNTS_PASS configuration.",
        )

    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
