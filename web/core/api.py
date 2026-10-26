"""
api.py — small helpers shared by every JSON endpoint.

The browser app talks to `/api/` with the session cookie plus an `X-CSRFToken`
header (Django's standard CSRF check stays on). Errors are always
`{"error": code, "detail": message}` with a matching status, so the frontend has
exactly one shape to handle.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, JsonResponse

from core.storage import StorageUnavailable


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", **extra: Any):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def error_response(status: int, code: str, detail: str = "", **extra: Any) -> JsonResponse:
    return JsonResponse({"error": code, "detail": detail, **extra}, status=status)


def parse_json(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError as exc:
        raise ApiError(400, "invalid_json", str(exc)) from exc
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "expected a JSON object")
    return data


def api_view(methods: list[str]) -> Callable:
    """Decorator for JSON endpoints: method check, authentication (the 2FA/approval
    gate in AccountGateMiddleware has already run by the time a view sees a
    request), and ApiError -> JSON. Views return a dict/list (serialized here) or
    an HttpResponse when they need to (file streams, 204s)."""

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any):
            if request.method not in methods:
                return error_response(405, "method_not_allowed", f"{request.method} not allowed")
            if not request.user.is_authenticated:
                return error_response(401, "not_authenticated", "Sign in again.")
            try:
                result = view(request, *args, **kwargs)
            except ApiError as exc:
                return error_response(exc.status, exc.code, exc.detail, **exc.extra)
            except StorageUnavailable as exc:
                return error_response(503, "storage_unavailable", str(exc))
            if isinstance(result, (dict, list)):
                return JsonResponse(result, safe=False)
            return result

        return wrapper

    return decorator
