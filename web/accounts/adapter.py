"""
adapter.py — where allauth's flows meet this site's rules.

- Request access is open (behind Turnstile), but a new account can't sign in
  until its email is verified *and* an admin approves it.
- Verifying an email lands on the "waiting for approval" page and tells the
  admins someone is waiting.
- Email rendering works without a request, so background workers can use the
  same templates and sender as the account emails.
"""

from __future__ import annotations

from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import reverse


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request) -> bool:
        return True

    def pre_login(self, request, user, **kwargs):
        if not user.is_active:
            return self.respond_user_inactive(request, user)
        # Unverified accounts fall through to allauth's own verification stage,
        # which re-sends the email; only a verified-but-unapproved account is
        # stopped here. This runs after the password check, so the pending page
        # is only ever shown to someone who knows the password.
        if user.email_verified() and not user.is_approved:
            return HttpResponseRedirect(reverse("accounts:pending"))
        return super().pre_login(request, user, **kwargs)

    def get_email_verification_redirect_url(self, email_address) -> str:
        if not email_address.user.is_approved:
            return reverse("accounts:pending")
        return super().get_email_verification_redirect_url(email_address)

    def send_mail(self, template_prefix: str, email: str, context: dict) -> None:
        """Same as allauth's, minus its dependency on a live request: the
        workers send job emails with no request at all, and the site's name and
        URL are fixed anyway rather than derived from the Host header."""
        from allauth.core import context as allauth_context

        ctx = {
            "request": allauth_context.request,
            "email": email,
            "site_name": "Cell Counts",
            "site_url": settings.SITE_URL,
        }
        ctx.update(context)
        self.render_mail(template_prefix, email, ctx).send()


class MFAAdapter(DefaultMFAAdapter):
    def get_totp_label(self, user) -> str:
        return user.email
