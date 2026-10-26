"""
models.py — users, their notification preferences, and the devices they've
signed in from.

Email is the login identity (no usernames). An account exists in four states,
derived rather than stored so they can't drift apart:

    pending verification  -> allauth EmailAddress not verified yet
    pending approval      -> verified, approved_at is NULL
    active                -> verified, approved, is_active
    disabled              -> is_active False (an admin switched it off)
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra) -> User:
        if not email:
            raise ValueError("email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("approved_at", timezone.now())
        return self._create(email, password, **extra)


def default_quota() -> int:
    return settings.CELLCOUNTS_DEFAULT_QUOTA_BYTES


class User(AbstractUser):
    username = None
    first_name = None
    last_name = None
    email = models.EmailField("email address", unique=True)
    display_name = models.CharField(max_length=80, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    storage_quota_bytes = models.BigIntegerField(default=default_quota)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None

    @property
    def label(self) -> str:
        """What other people see: the display name if set, else the part of the
        email before the @ (never the full address — collaborators don't need it)."""
        return self.display_name or self.email.split("@", 1)[0]

    def email_verified(self) -> bool:
        from allauth.account.models import EmailAddress

        return EmailAddress.objects.filter(user=self, email__iexact=self.email, verified=True).exists()

    def has_mfa(self) -> bool:
        from allauth.mfa.models import Authenticator

        return Authenticator.objects.filter(
            user=self, type__in=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN]).exists()

    def account_state(self) -> str:
        if not self.is_active:
            return "disabled"
        if not self.email_verified():
            return "pending_verification"
        if not self.is_approved:
            return "pending_approval"
        return "active"

    def storage_used_bytes(self) -> int:
        """Originals only — tiles and exports are system overhead, not the user's
        data. Counts images in projects this user owns, whoever uploaded them,
        plus space reserved by uploads still in flight."""
        from projects.models import Image
        from uploads.models import UploadSession

        stored = Image.objects.filter(project__owner=self).aggregate(total=models.Sum("size_bytes"))["total"]
        reserved = UploadSession.objects.filter(
            project__owner=self, completed_at__isnull=True).aggregate(total=models.Sum("size_bytes"))["total"]
        return (stored or 0) + (reserved or 0)


class NotificationPreferences(models.Model):
    """Opt-in job emails. Security emails (password, 2FA, new device) aren't
    represented here because they can't be turned off."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="notification_prefs")
    batch_finished = models.BooleanField(
        default=False, help_text="When your last queued image in a project finishes.")
    job_failures = models.BooleanField(
        default=False, help_text="As soon as one of your images fails to process.")
    daily_summary = models.BooleanField(
        default=False, help_text="Once a day, if any of your images were processed.")
    last_daily_summary_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"notifications for {self.user}"

    @classmethod
    def for_user(cls, user: User) -> NotificationPreferences:
        prefs, _ = cls.objects.get_or_create(user=user)
        return prefs


class KnownDevice(models.Model):
    """A (user-agent, network) pair a user has signed in from before. Signing in
    from a pair not in this table sends a "new sign-in" security email. The IP
    is reduced to its /24 (or /48 for IPv6) before it's ever stored, so ordinary
    DHCP churn on the same network doesn't count as a new device and no full
    client address is kept."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="known_devices")
    fingerprint = models.CharField(max_length=64)
    user_agent = models.CharField(max_length=300)
    network = models.CharField(max_length=64)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "fingerprint"], name="unique_device_per_user")]

    def __str__(self) -> str:
        return f"{self.user_agent} ({self.network}) for {self.user}"

    @staticmethod
    def make_fingerprint(user_agent: str, network: str) -> str:
        return hashlib.sha256(f"{user_agent}\n{network}".encode()).hexdigest()
