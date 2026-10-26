from __future__ import annotations

from allauth.account.internal.flows.login import record_authentication
from allauth.account.signals import email_confirmed, user_logged_in
from django.dispatch import receiver

from accounts import services


@receiver(email_confirmed)
def on_email_confirmed(sender, request, email_address, **kwargs):
    user = email_address.user
    if not user.is_approved:
        services.notify_admins_of_pending(user)


@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    services.record_sign_in(request, user)
    if not user.has_mfa():
        # allauth asks for the password again before adding an authenticator
        # unless the session holds a *re*authentication. Someone without 2FA
        # who has just signed in with their password is about to be sent
        # straight to 2FA setup, so asking for the password a second time a few
        # seconds later is pure friction; record this sign-in as fresh. (Once
        # 2FA exists, later changes to it still need a real reauthentication.)
        record_authentication(request, user, method="password", reauthenticated=True)
