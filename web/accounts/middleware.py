"""
middleware.py — the gate every signed-in request passes through.

1. An account that has been disabled or un-approved since its session started
   is signed out immediately; no waiting for the session to expire.
2. Mandatory 2FA: a signed-in user with no authenticator (TOTP app or passkey)
   can reach only the 2FA setup pages and sign-out until they've set one up.
   API calls get a JSON 403 instead of a redirect.
3. Staff-only areas (/manage/, /django-admin/) require is_staff.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect

# Reachable while 2FA isn't set up yet. Everything under /accounts/2fa/ is
# allauth's MFA management (activate TOTP, add a passkey, recovery codes), plus
# whatever allauth needs to reauthenticate or sign out.
MFA_SETUP_ALLOWED_PREFIXES = (
    "/accounts/2fa/",
    "/accounts/logout/",
    "/accounts/reauthenticate/",
    "/static/",
)
STAFF_PREFIXES = ("/manage/", "/django-admin/")


class AccountGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if not user.is_authenticated:
            return self.get_response(request)

        path = request.path
        is_api = path.startswith("/api/")

        if not user.is_active or not user.is_approved:
            logout(request)
            if is_api:
                return JsonResponse({"error": "not_authenticated", "detail": "Sign in again."}, status=401)
            return redirect("account_login")

        if not user.has_mfa() and not path.startswith(MFA_SETUP_ALLOWED_PREFIXES):
            if is_api:
                return JsonResponse(
                    {"error": "mfa_required", "detail": "Set up two-factor authentication first."},
                    status=403)
            messages.info(request, "Set up two-factor authentication to continue.")
            return redirect("mfa_activate_totp")

        if path.startswith(STAFF_PREFIXES) and not user.is_staff:
            return redirect("projects:list")

        return self.get_response(request)
