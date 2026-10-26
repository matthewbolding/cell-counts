from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.core.cache import cache
from django.utils import timezone

from accounts.models import User

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def data_root(tmp_path, settings):
    """Every test gets its own empty data volume (with the marker file that
    core.storage requires)."""
    root = tmp_path / "data"
    root.mkdir()
    (root / ".cellcounts-volume").touch()
    settings.CELLCOUNTS_DATA_ROOT = root
    return root


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Tests never call Cloudflare: the token "ok" passes, anything else fails.
    monkeypatch.setattr("core.turnstile.verify", lambda token, remote_ip: token == "ok")
    cache.clear()  # allauth rate limits live in the cache
    yield
    cache.clear()


@pytest.fixture
def make_user(db):
    def make(email: str = "alice@example.com", *, approved: bool = True, verified: bool = True,
             mfa: bool = True, staff: bool = False, password: str | None = PASSWORD, **extra) -> User:
        user = User.objects.create_user(
            email=email, password=password, is_staff=staff,
            approved_at=timezone.now() if approved else None, **extra)
        EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
        if mfa:
            Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={"secret": "test"})
        return user

    return make


@pytest.fixture
def user(make_user) -> User:
    return make_user()


@pytest.fixture
def admin_user(make_user) -> User:
    return make_user("admin@example.com", staff=True)


@pytest.fixture
def login(client):
    """Signed-in client for a user who has completed sign-in (incl. 2FA)."""

    def _login(user: User):
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return client

    return _login
