"""HTTP Basic auth, checked against env-configured credentials.

Single preassigned username/password for the whole tool — there is exactly one
reviewer, and this endpoint is only reachable at all because it's proxied through
Cloudflare + Nginx Proxy Manager over HTTPS, so Basic auth's plaintext-over-the-wire
weakness doesn't apply.
"""

from __future__ import annotations

import base64
import os
import secrets

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_security = HTTPBasic()


def check_credentials(username: str, password: str) -> bool:
    expected_user = os.environ.get("CELLCOUNTS_USER")
    expected_pass = os.environ.get("CELLCOUNTS_PASS")
    if not expected_user or not expected_pass:
        return False
    user_ok = secrets.compare_digest(username, expected_user)
    pass_ok = secrets.compare_digest(password, expected_pass)
    return user_ok and pass_ok


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    if os.environ.get("CELLCOUNTS_USER") is None or os.environ.get("CELLCOUNTS_PASS") is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server is missing CELLCOUNTS_USER/CELLCOUNTS_PASS configuration.",
        )
    if not check_credentials(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_auth_ws(websocket: WebSocket) -> str | None:
    """Basic-auth check for the /ws/jobs route. FastAPI's `Depends(HTTPBasic())`
    only works against an HTTP `Request` -- calling it on a websocket route
    raises `TypeError: HTTPBasic.__call__() missing 1 required positional
    argument: 'request'` at connection time (confirmed against this repo's
    installed fastapi/starlette versions), so the header has to be parsed by
    hand here instead. Returns the username on success, None on failure --
    caller is responsible for closing the connection (before accept()) rather
    than raising an HTTPException, since that's not meaningful once a
    websocket handshake is underway.
    """
    auth_header = websocket.headers.get("authorization", "")
    if not auth_header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if not check_credentials(username, password):
        return None
    return username
