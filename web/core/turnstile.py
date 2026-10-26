"""
turnstile.py — Cloudflare Turnstile verification for the public forms (sign in,
request access, password reset request).

Verification is server-to-server against Cloudflare's siteverify endpoint. A
network failure counts as a failed check: the forms that use this are exactly the
ones bots go after, so failing closed is the right default.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django import forms
from django.conf import settings

log = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
RESPONSE_FIELD = "cf-turnstile-response"


def verify(token: str, remote_ip: str | None) -> bool:
    if not token:
        return False
    payload = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    req = urllib.request.Request(
        SITEVERIFY_URL, data=urllib.parse.urlencode(payload).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning("Turnstile verification failed to complete: %s", exc)
        return False
    return bool(result.get("success"))


class TurnstileWidget(forms.Widget):
    template_name = "core/turnstile_widget.html"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["site_key"] = settings.TURNSTILE_SITE_KEY
        return context

    def value_from_datadict(self, data, files, name):
        return data.get(RESPONSE_FIELD, "")


class TurnstileFormMixin:
    """Adds a Turnstile challenge to a form. allauth only hands some of its forms
    the request (login does, signup and password reset don't), so fall back to
    the request allauth's middleware keeps in a context variable."""

    turnstile_error = "Please complete the verification challenge and try again."

    def add_turnstile_field(self) -> None:
        self.fields["turnstile"] = forms.CharField(
            widget=TurnstileWidget, required=False, label="")

    def clean_turnstile(self) -> str:
        from allauth.account.adapter import get_adapter
        from allauth.core import context

        token = self.cleaned_data.get("turnstile", "")
        request = getattr(self, "request", None) or context.request
        remote_ip = None
        if request is not None:
            remote_ip = get_adapter(request).get_client_ip(request)
        if not verify(token, remote_ip):
            raise forms.ValidationError(self.turnstile_error)
        return token
