"""
services.py — account actions an admin takes from the user portal, and the
security emails that go with them. Views stay thin; everything that changes an
account, and every email that goes out because of it, is here.
"""

from __future__ import annotations

import ipaddress
import re

from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import KnownDevice, User
from accounts.tokens import ResetTokenGenerator


def absolute(path: str) -> str:
    return settings.SITE_URL.rstrip("/") + path


def _send(template: str, user: User, **context) -> None:
    get_adapter().send_mail(f"accounts/email/{template}", user.email, {"user": user, **context})


def end_all_sessions(user: User) -> None:
    for session in UserSession.objects.filter(user=user):
        session.end()


def password_link(user: User) -> str:
    token = ResetTokenGenerator().make_token(user)
    path = reverse("account_reset_password_from_key",
                   kwargs={"uidb36": user_pk_to_url_str(user), "key": token})
    return absolute(path)


@transaction.atomic
def approve(user: User, by: User) -> None:
    user.approved_at = timezone.now()
    user.approved_by = by
    user.save(update_fields=["approved_at", "approved_by"])
    _send("approved", user, login_url=absolute(reverse("account_login")))


@transaction.atomic
def reject(user: User) -> None:
    """Only for accounts that were never approved: there's nothing of theirs to
    keep, so the row goes away entirely."""
    if user.is_approved:
        raise ValueError("reject() is for pending accounts; disable approved ones instead")
    user.delete()


def set_active(user: User, active: bool) -> None:
    user.is_active = active
    user.save(update_fields=["is_active"])
    if not active:
        end_all_sessions(user)


def invite(email: str, display_name: str, quota_bytes: int, by: User) -> User:
    """Create an approved account with no password and email a set-password
    link. The new user still has to set up 2FA on first sign-in. If the email
    fails to send, the account still exists and "Resend Invite" retries it."""
    with transaction.atomic():
        user = User.objects.create_user(
            email=email, display_name=display_name, storage_quota_bytes=quota_bytes,
            approved_at=timezone.now(), approved_by=by)
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    _send("invite", user, invited_by=by, set_password_url=password_link(user))
    return user


def resend_invite(user: User, by: User) -> None:
    _send("invite", user, invited_by=by, set_password_url=password_link(user))


def reset_mfa(user: User, by: User) -> None:
    """The recovery path for a lost authenticator *and* lost recovery codes:
    an admin removes every authenticator, which signs the user out everywhere.
    Their next sign-in (with their password) goes straight to 2FA setup."""
    Authenticator.objects.filter(user=user).delete()
    end_all_sessions(user)
    _send("mfa_reset", user, admin=by)


def notify_admins_of_pending(user: User) -> None:
    review_url = absolute(reverse("accounts:manage_users"))
    for admin in User.objects.filter(is_staff=True, is_active=True):
        get_adapter().send_mail(
            "accounts/email/pending_approval", admin.email,
            {"user": admin, "applicant": user, "review_url": review_url})


# --------------------------------------------------------------------------- #
# New-device sign-in emails
# --------------------------------------------------------------------------- #
_BROWSERS = [("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
             ("Safari/", "Safari")]
_SYSTEMS = [("Windows", "Windows"), ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("iPhone", "iOS"),
            ("iPad", "iPadOS"), ("Android", "Android"), ("CrOS", "ChromeOS"), ("Linux", "Linux")]


def describe_user_agent(user_agent: str) -> str:
    browser = next((name for token, name in _BROWSERS if token in user_agent), "Unknown browser")
    system = next((name for token, name in _SYSTEMS if token in user_agent), "unknown system")
    return f"{browser} on {system}"


def network_of(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def record_sign_in(request, user: User) -> None:
    """Called once a sign-in has fully completed (password and 2FA). The very
    first device an account ever uses is recorded silently; after that, a new
    browser/network combination gets an email."""
    raw_ua = request.META.get("HTTP_USER_AGENT", "")
    # Drop version numbers so a browser update isn't a "new device".
    ua_family = re.sub(r"[\d.]+", "", raw_ua)[:300]
    network = network_of(get_adapter(request).get_client_ip(request))
    fingerprint = KnownDevice.make_fingerprint(ua_family, network)

    had_devices = user.known_devices.exists()
    device, created = KnownDevice.objects.get_or_create(
        user=user, fingerprint=fingerprint,
        defaults={"user_agent": describe_user_agent(raw_ua), "network": network})
    if not created:
        device.save(update_fields=["last_seen"])
    elif had_devices:
        _send("new_device", user, device=device, when=timezone.now(),
              reset_url=absolute(reverse("account_reset_password")))
