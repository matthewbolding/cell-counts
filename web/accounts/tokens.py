"""
tokens.py — password-reset tokens with two lifetimes.

Invites reuse allauth's password-reset-from-key flow (it already has the
"GET shows a form, only POST acts" property that keeps email link scanners from
burning the token). Django has a single global PASSWORD_RESET_TIMEOUT, though,
and an invite has to survive a long weekend where a password reset shouldn't
outlive the afternoon. The token embeds its timestamp, so this generator picks
the limit at check time: an account that has never signed in and has no
password yet is an invite (7 days); anything else is a reset (2 hours).

Tokens die on first use either way: the hash covers the password hash and last
login, both of which change when the link is used.
"""

from __future__ import annotations

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.crypto import constant_time_compare
from django.utils.http import base36_to_int

INVITE_TIMEOUT_SECONDS = 7 * 24 * 3600
RESET_TIMEOUT_SECONDS = 2 * 3600


def is_pending_invite(user) -> bool:
    return user.last_login is None and not user.has_usable_password()


class ResetTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp) -> str:
        # Same as allauth's EmailAwarePasswordResetTokenGenerator: a token stops
        # working if the account's email addresses change after it was issued.
        from allauth.account.models import EmailAddress

        emails = set(EmailAddress.objects.filter(user_id=user.pk).values_list("email", flat=True))
        emails.add(user.email)
        return super()._make_hash_value(user, timestamp) + "|".join(sorted(emails))

    def check_token(self, user, token) -> bool:
        if not (user and token):
            return False
        try:
            ts_b36, _ = token.split("-")
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False
        for secret in [self.secret, *self.secret_fallbacks]:
            if constant_time_compare(self._make_token_with_timestamp(user, ts, secret), token):
                break
        else:
            return False
        limit = INVITE_TIMEOUT_SECONDS if is_pending_invite(user) else RESET_TIMEOUT_SECONDS
        return (self._num_seconds(self._now()) - ts) <= limit
