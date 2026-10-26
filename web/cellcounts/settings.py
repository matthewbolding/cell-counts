"""
Django settings for the Cell Counts web app.

Everything deployment-specific comes from the environment (see deploy/.env.example);
nothing secret is ever committed. `CELLCOUNTS_ENV=dev` relaxes exactly the things
that can't work on a laptop (HTTPS-only cookies, real email, real Turnstile keys) and
nothing else — the security middleware, rate limits and 2FA enforcement all run the
same way in development as in production, so they get exercised every day instead
of only after a deploy.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"{name} must be set")
    return value or ""


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


ENVIRONMENT = env("CELLCOUNTS_ENV", "production")
DEV = ENVIRONMENT == "dev"
DEBUG = DEV and env_bool("DJANGO_DEBUG", True)

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key" if DEV else None, required=not DEV)
SITE_URL = env("CELLCOUNTS_SITE_URL", "http://localhost:8000" if DEV else "https://cellcounts.matthewbolding.com")
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
                 if h.strip()]
CSRF_TRUSTED_ORIGINS = [SITE_URL]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.postgres",
    "django.forms",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.usersessions",
    "anymail",
    "core",
    "accounts",
    "projects",
    "uploads",
    "processing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "allauth.usersessions.middleware.UserSessionsMiddleware",
    "accounts.middleware.AccountGateMiddleware",
]

ROOT_URLCONF = "cellcounts.urls"
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"  # widgets can use templates/ overrides
WSGI_APPLICATION = "cellcounts.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.csp",
                "core.context_processors.site",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "cellcounts"),
        "USER": env("POSTGRES_USER", "cellcounts"),
        "PASSWORD": env("POSTGRES_PASSWORD", "cellcounts" if DEV else None, required=not DEV),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "54329" if DEV else "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

# --------------------------------------------------------------------------- #
# Passwords and sessions
# --------------------------------------------------------------------------- #
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEV
SESSION_COOKIE_NAME = "cellcounts_session" if DEV else "__Host-cellcounts_session"
CSRF_COOKIE_SECURE = not DEV
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_NAME = "cellcounts_csrf" if DEV else "__Host-cellcounts_csrf"

# --------------------------------------------------------------------------- #
# Transport / headers. TLS terminates at Cloudflare; cloudflared -> nginx -> here
# is plain HTTP on a private Docker network, and nginx forwards the original
# scheme. Only nginx can reach gunicorn, so trusting X-Forwarded-Proto is safe.
# --------------------------------------------------------------------------- #
if not DEV:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False  # don't make that decision for all of matthewbolding.com
    # ...and the same goes for the HSTS preload list, so silence the checks that ask for both.
    SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

from django.utils.csp import CSP  # noqa: E402

SECURE_CSP = {
    "default-src": [CSP.SELF],
    # wasm-unsafe-eval lets the upload page's hashing worker (hash-wasm) compile
    # WebAssembly; it does not permit eval() of JavaScript.
    "script-src": [CSP.SELF, CSP.NONCE, "'wasm-unsafe-eval'", "https://challenges.cloudflare.com"],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "connect-src": [CSP.SELF],
    "frame-src": ["https://challenges.cloudflare.com"],
    "worker-src": [CSP.SELF, "blob:"],
    "font-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

# Uploads stream chunk bodies straight to disk (uploads/views.py reads
# request.stream), so this limit only bounds ordinary form/JSON posts. The
# review-file import posts a gzipped cells document and raises its own cap.
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --------------------------------------------------------------------------- #
# allauth
# --------------------------------------------------------------------------- #
ACCOUNT_ADAPTER = "accounts.adapter.AccountAdapter"
MFA_ADAPTER = "accounts.adapter.MFAAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_CONFIRM_EMAIL_ON_GET = False  # link scanners must not be able to verify for you
ACCOUNT_EMAIL_NOTIFICATIONS = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = False
ACCOUNT_LOGIN_ON_PASSWORD_RESET = False  # a reset never skips the 2FA step
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_EMAIL_SUBJECT_PREFIX = "Cell Counts — "
ACCOUNT_SESSION_REMEMBER = None
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = False
ACCOUNT_PASSWORD_RESET_TOKEN_GENERATOR = "accounts.tokens.ResetTokenGenerator"
ACCOUNT_CHANGE_EMAIL = False  # email is the identity an admin approved; changing it goes through an admin
ACCOUNT_FORMS = {
    "login": "accounts.forms.LoginForm",
    "signup": "accounts.forms.SignupForm",
    "reset_password": "accounts.forms.ResetPasswordForm",
}
ACCOUNT_RATE_LIMITS = {
    "login": "20/m/ip,100/h/ip",
    "login_failed": "5/10m/ip,5/10m/key",
    "signup": "5/h/ip",
    "confirm_email": "1/3m/key",
    "reset_password": "5/h/ip,3/h/key",
    "reset_password_from_key": "20/m/ip",
    "change_password": "5/m/user",
    "reauthenticate": "10/m/user",
}
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "projects:list"
ACCOUNT_LOGOUT_REDIRECT_URL = "account_login"

MFA_SUPPORTED_TYPES = ["totp", "recovery_codes", "webauthn"]
MFA_PASSKEY_LOGIN_ENABLED = False
MFA_TOTP_ISSUER = "Cell Counts"
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = DEV
MFA_TRUST_ENABLED = False  # always ask; the "remember this browser" shortcut is how 2FA gets skipped
if DEV and env_bool("CELLCOUNTS_DEV_TOTP_BYPASS"):
    # Lets local browser testing get past 2FA with code 000000. allauth itself
    # refuses this setting unless DEBUG is on, so it can't leak into production.
    MFA_TOTP_INSECURE_BYPASS_CODE = "000000"

# Cloudflare is the only hop that sees the real client; behind the tunnel every
# request comes from cloudflared's container. nginx passes CF-Connecting-IP
# through and refuses requests that didn't come via the tunnel.
ALLAUTH_TRUSTED_CLIENT_IP_HEADER = None if DEV else "CF-Connecting-IP"

# --------------------------------------------------------------------------- #
# Email (Resend via anymail; console in dev)
# --------------------------------------------------------------------------- #
DEFAULT_FROM_EMAIL = env("CELLCOUNTS_FROM_EMAIL", "Cell Counts <cellcounts@matthewbolding.com>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
if env("RESEND_API_KEY"):
    MAILERS = {"default": {"BACKEND": "anymail.backends.resend.EmailBackend",
                           "OPTIONS": {"api_key": env("RESEND_API_KEY")}}}
elif DEV:
    MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
else:
    raise ImproperlyConfigured("RESEND_API_KEY must be set in production")

# --------------------------------------------------------------------------- #
# Turnstile. Dev defaults are Cloudflare's documented always-pass test keys.
# --------------------------------------------------------------------------- #
TURNSTILE_SITE_KEY = env("TURNSTILE_SITE_KEY", "1x00000000000000000000AA" if DEV else None, required=not DEV)
TURNSTILE_SECRET_KEY = env(
    "TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA" if DEV else None, required=not DEV)

# --------------------------------------------------------------------------- #
# Storage: the NAS mount. See core/storage.py for the volume-marker check.
# --------------------------------------------------------------------------- #
CELLCOUNTS_DATA_ROOT = Path(env("CELLCOUNTS_DATA_ROOT", str(BASE_DIR / ".data") if DEV else "/data"))
CELLCOUNTS_DEFAULT_QUOTA_BYTES = env_int("CELLCOUNTS_DEFAULT_QUOTA_GB", 150) * 1024**3
CELLCOUNTS_UPLOAD_CHUNK_SIZE = 32 * 1024 * 1024
# nginx serves tiles/exports itself after Django authorizes the request
# (X-Accel-Redirect). In dev there's no nginx, so Django streams the file.
CELLCOUNTS_USE_X_ACCEL = not DEV

# --------------------------------------------------------------------------- #
# Static files / i18n
# --------------------------------------------------------------------------- #
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
                    if not DEV else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
VITE_MANIFEST_PATH = BASE_DIR / "static" / "dist" / ".vite" / "manifest.json"

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TZ", "America/Chicago")
USE_I18N = True
USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.security": {"level": "WARNING"}},
}

# --- deploy settings ---
# gunicorn runs several worker processes, and allauth's rate limits (plus the
# tile-permission and composite caches) live in Django's cache. The default
# LocMemCache is per process, which would give each gunicorn worker its own
# rate-limit counters and multiply every limit by the worker count. A database
# cache is shared by all of them; deploy/release.sh runs `createcachetable`.
if not DEV:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "django_cache"}}
