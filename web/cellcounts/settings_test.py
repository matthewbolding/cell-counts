"""Test settings: development defaults plus fast hashing and in-memory email."""

import os

os.environ.setdefault("CELLCOUNTS_ENV", "dev")
os.environ.setdefault("CELLCOUNTS_FAKE_MODEL", "1")

from cellcounts.settings import *  # noqa: E402,F403

MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Parallel test runs (several agents/terminals) each need their own database.
DATABASES["default"]["TEST"] = {"NAME": os.environ.get("CELLCOUNTS_TEST_DB", "test_cellcounts")}  # noqa: F405
