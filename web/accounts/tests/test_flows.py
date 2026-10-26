"""
End-to-end account flows through the real allauth views: request access,
email verification, admin approval, mandatory 2FA, password reset, invites,
admin actions, and the new-device security email.
"""

from __future__ import annotations

import re

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.core import mail
from django.core.cache import cache
from django.urls import reverse

from accounts.models import KnownDevice, User
from accounts.tokens import RESET_TIMEOUT_SECONDS, ResetTokenGenerator
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

LOGIN = "/accounts/login/"


def _links(message) -> list[str]:
    return re.findall(r"https?://\S+", message.body)


def _sign_in(client, email, password=PASSWORD, turnstile="ok", **extra):
    data = {"login": email, "password": password, "cf-turnstile-response": turnstile, **extra}
    return client.post(LOGIN, data)


# --------------------------------------------------------------------------- #
# Request access -> verify -> approve -> 2FA
# --------------------------------------------------------------------------- #
def test_request_access_requires_turnstile(client):
    response = client.post("/accounts/signup/", {
        "email": "new@example.com", "password1": PASSWORD, "password2": PASSWORD,
        "cf-turnstile-response": "nope"})
    assert response.status_code == 200
    assert "verification challenge" in response.content.decode()
    assert not User.objects.filter(email="new@example.com").exists()


def test_full_request_access_flow(client, admin_user, settings):
    response = client.post("/accounts/signup/", {
        "display_name": "New Person", "email": "new@example.com", "password1": PASSWORD,
        "password2": PASSWORD, "cf-turnstile-response": "ok"})
    assert response.status_code == 302
    user = User.objects.get(email="new@example.com")
    assert user.display_name == "New Person"
    assert user.approved_at is None
    assert not client.session.get("_auth_user_id")

    # The confirmation link only shows a page on GET; scanners can't confirm.
    confirm_link = next(link for link in _links(mail.outbox[-1]) if "/confirm-email/" in link)
    path = confirm_link.split("://", 1)[1].split("/", 1)[1]
    response = client.get("/" + path)
    assert response.status_code == 200
    assert not EmailAddress.objects.get(email="new@example.com").verified

    mail.outbox.clear()
    response = client.post("/" + path)
    assert response.status_code == 302
    assert response.url == reverse("accounts:pending")
    assert EmailAddress.objects.get(email="new@example.com").verified
    # Admins are told someone is waiting.
    assert [m.to for m in mail.outbox] == [[admin_user.email]]
    assert "new@example.com" in mail.outbox[0].subject

    # Verified but unapproved: the correct password lands on the pending page,
    # and no session is created.
    response = _sign_in(client, "new@example.com")
    assert response.status_code == 302 and response.url == reverse("accounts:pending")
    assert not client.session.get("_auth_user_id")

    # Admin approves from the portal; the applicant is emailed.
    admin_client = client.__class__()
    admin_client.force_login(admin_user, backend="django.contrib.auth.backends.ModelBackend")
    mail.outbox.clear()
    response = admin_client.post(reverse("accounts:manage_user_action", args=[user.pk, "approve"]))
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.approved_by == admin_user
    assert mail.outbox[0].to == ["new@example.com"]

    # First sign-in: no authenticator yet, so everything redirects to 2FA setup.
    response = _sign_in(client, "new@example.com")
    assert response.status_code == 302
    response = client.get(reverse("projects:list"))
    assert response.status_code == 302 and response.url == reverse("mfa_activate_totp")
    api = client.get("/api/queue/")
    assert api.status_code == 403 and api.json()["error"] == "mfa_required"
    assert client.get(reverse("mfa_activate_totp")).status_code == 200


def test_wrong_turnstile_blocks_sign_in(client, user):
    response = _sign_in(client, user.email, turnstile="bad")
    assert response.status_code == 200
    assert not client.session.get("_auth_user_id")


def test_sign_in_with_totp(client, make_user):
    user = make_user("totp@example.com", mfa=False)
    secret = generate_totp_secret()
    TOTP.activate(user, secret)

    response = _sign_in(client, user.email)
    assert response.status_code == 302 and response.url == reverse("mfa_authenticate")
    assert not client.session.get("_auth_user_id")

    wrong = client.post(reverse("mfa_authenticate"), {"code": "000000"})
    assert wrong.status_code == 200
    assert not client.session.get("_auth_user_id")

    code = format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))
    response = client.post(reverse("mfa_authenticate"), {"code": code})
    assert response.status_code == 302
    assert client.session.get("_auth_user_id") == str(user.pk)
    assert client.get(reverse("projects:list")).status_code == 200


def test_unapproved_or_disabled_session_is_ended(login, user):
    client = login(user)
    assert client.get(reverse("projects:list")).status_code == 200
    user.is_active = False
    user.save()
    # Django already treats an inactive user's session as anonymous; the gate
    # also handles approval being revoked, which Django knows nothing about.
    response = client.get(reverse("projects:list"))
    assert response.status_code == 302 and response.url.startswith(reverse("account_login"))
    user.is_active = True
    user.approved_at = None
    user.save()
    client = login(user)
    response = client.get(reverse("projects:list"))
    assert response.status_code == 302 and response.url == reverse("account_login")
    assert not client.session.get("_auth_user_id")


def test_staff_pages_need_staff(login, user):
    client = login(user)
    response = client.get(reverse("accounts:manage_users"))
    assert response.status_code == 302 and response.url == reverse("projects:list")
    assert client.get("/django-admin/").status_code == 302


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #
def test_password_reset_does_not_sign_in_or_skip_2fa(client, user):
    response = client.post("/accounts/password/reset/", {"email": user.email, "cf-turnstile-response": "ok"})
    assert response.status_code == 302
    link = next(link for link in _links(mail.outbox[-1]) if "/password/reset/key/" in link)
    path = "/" + link.split("://", 1)[1].split("/", 1)[1]

    # GET (what a mail scanner does) doesn't use the token up.
    response = client.get(path, follow=True)
    assert response.status_code == 200
    form_url = response.redirect_chain[-1][0] if response.redirect_chain else path
    response = client.get(form_url)
    assert response.status_code == 200

    new_password = "a brand new passphrase 42"
    response = client.post(form_url, {"password1": new_password, "password2": new_password})
    assert response.status_code == 302
    assert not client.session.get("_auth_user_id")  # ACCOUNT_LOGIN_ON_PASSWORD_RESET = False

    # Signing in with the new password still goes through the 2FA stage.
    response = _sign_in(client, user.email, password=new_password)
    assert response.status_code == 302 and response.url == reverse("mfa_authenticate")


def test_password_reset_requires_turnstile(client, user):
    response = client.post("/accounts/password/reset/", {"email": user.email, "cf-turnstile-response": "x"})
    assert response.status_code == 200
    assert not mail.outbox


def test_reset_tokens_expire_but_invites_last_longer(user, make_user, monkeypatch):
    gen = ResetTokenGenerator()
    token = gen.make_token(user)
    real_now = gen._now()
    monkeypatch.setattr(ResetTokenGenerator, "_now", lambda self: real_now)
    assert gen.check_token(user, token)

    from datetime import timedelta

    monkeypatch.setattr(
        ResetTokenGenerator, "_now", lambda self: real_now + timedelta(seconds=RESET_TIMEOUT_SECONDS + 60))
    assert not gen.check_token(user, token)

    invited = make_user("invited@example.com", password=None, mfa=False)
    invited.last_login = None
    invited.save()
    monkeypatch.setattr(ResetTokenGenerator, "_now", lambda self: real_now)
    invite_token = gen.make_token(invited)
    monkeypatch.setattr(ResetTokenGenerator, "_now", lambda self: real_now + timedelta(days=6))
    assert gen.check_token(invited, invite_token)
    monkeypatch.setattr(ResetTokenGenerator, "_now", lambda self: real_now + timedelta(days=8))
    assert not gen.check_token(invited, invite_token)


# --------------------------------------------------------------------------- #
# Admin portal
# --------------------------------------------------------------------------- #
def test_invite_creates_approved_account_with_set_password_link(login, admin_user):
    admin = login(admin_user)
    response = admin.post(reverse("accounts:manage_invite"), {
        "email": "Invitee@Example.com", "display_name": "Invitee", "quota_gb": 50})
    assert response.status_code == 302
    invitee = User.objects.get(email="invitee@example.com")
    assert invitee.is_approved and not invitee.has_usable_password()
    assert invitee.storage_quota_bytes == 50 * 1024**3
    assert EmailAddress.objects.get(user=invitee).verified
    message = mail.outbox[-1]
    assert message.to == ["invitee@example.com"]
    link = next(link for link in _links(message) if "/password/reset/key/" in link)
    assert link.startswith("http://localhost:8000/")


def test_invite_rejects_existing_email(login, admin_user, user):
    admin = login(admin_user)
    admin.post(reverse("accounts:manage_invite"), {"email": user.email, "quota_gb": 10})
    assert User.objects.filter(email=user.email).count() == 1
    assert not mail.outbox


def test_reset_mfa_removes_authenticators_and_emails(login, admin_user, user):
    admin = login(admin_user)
    response = admin.post(reverse("accounts:manage_user_action", args=[user.pk, "reset_mfa"]))
    assert response.status_code == 302
    assert not Authenticator.objects.filter(user=user).exists()
    assert mail.outbox[-1].to == [user.email]
    assert "two-factor" in mail.outbox[-1].subject.lower()


def test_admin_cannot_disable_self(login, admin_user):
    admin = login(admin_user)
    admin.post(reverse("accounts:manage_user_action", args=[admin_user.pk, "disable"]))
    admin_user.refresh_from_db()
    assert admin_user.is_active


def test_quota_update(login, admin_user, user):
    admin = login(admin_user)
    admin.post(reverse("accounts:manage_user_action", args=[user.pk, "quota"]), {"quota_gb": 300})
    user.refresh_from_db()
    assert user.storage_quota_bytes == 300 * 1024**3


def test_reject_only_pending(login, admin_user, user, make_user):
    admin = login(admin_user)
    pending = make_user("pending@example.com", approved=False, mfa=False)
    admin.post(reverse("accounts:manage_user_action", args=[pending.pk, "reject"]))
    assert not User.objects.filter(pk=pending.pk).exists()
    admin.post(reverse("accounts:manage_user_action", args=[user.pk, "reject"]))
    assert User.objects.filter(pk=user.pk).exists()


# --------------------------------------------------------------------------- #
# Settings and new-device emails
# --------------------------------------------------------------------------- #
def test_notification_preferences_default_off_and_save(login, user):
    client = login(user)
    response = client.get(reverse("accounts:settings"))
    assert response.status_code == 200
    prefs = user.notification_prefs
    assert not (prefs.batch_finished or prefs.job_failures or prefs.daily_summary)
    client.post(reverse("accounts:settings"), {"save_notifications": "1", "job_failures": "on"})
    prefs.refresh_from_db()
    assert prefs.job_failures and not prefs.batch_finished


def test_new_device_email(client, make_user):
    user = make_user("devices@example.com", mfa=False)
    secret = generate_totp_secret()
    TOTP.activate(user, secret)

    def full_sign_in(user_agent: str):
        cache.clear()  # forget the previous code: allauth refuses to accept one twice
        c = client.__class__(HTTP_USER_AGENT=user_agent)
        _sign_in(c, user.email)
        code = format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))
        c.post(reverse("mfa_authenticate"), {"code": code})
        assert c.session.get("_auth_user_id") == str(user.pk)

    mac_chrome = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/140.0.0.0 Safari/537.36"
    win_firefox = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"

    full_sign_in(mac_chrome)
    assert KnownDevice.objects.filter(user=user).count() == 1
    assert not [m for m in mail.outbox if "New sign-in" in m.subject]

    full_sign_in(mac_chrome.replace("140.0.0.0", "141.0.0.0"))  # browser update: same device
    assert not [m for m in mail.outbox if "New sign-in" in m.subject]

    full_sign_in(win_firefox)
    notices = [m for m in mail.outbox if "New sign-in" in m.subject]
    assert len(notices) == 1 and "Firefox on Windows" in notices[0].body


def test_emails_render_without_a_request(user):
    """Workers send email with no request in flight."""
    from allauth.account.adapter import get_adapter

    get_adapter().send_mail("accounts/email/approved", user.email, {"user": user, "login_url": "x"})
    assert mail.outbox[-1].subject == "Cell Counts — Your access has been approved"
    assert "http://localhost:8000" in mail.outbox[-1].body
