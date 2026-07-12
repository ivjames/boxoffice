"""
Production settings for the lab980 droplet (gunicorn under pm2 behind nginx).

Database: defaults to SQLite in the app-dir `data/` — matching the lab980
one-dir-per-site convention (config + data live in /var/www/boxoffice, no extra
infra). harden_sqlite() makes concurrent booking safe (IMMEDIATE write lock +
wait-on-contention). Postgres is a drop-in upgrade: set DATABASE_URL to a
postgres:// URL and nothing else changes (select_for_update() then does real
row locking). All booking code runs in transaction.atomic() either way.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, env, harden_sqlite

DEBUG = False

# base.py's SECRET_KEY has a default ("insecure-dev-key-do-not-use-in-prod")
# so a bare `pip install && migrate && runserver` works with zero setup in
# dev. That fallback must never reach prod: it's a fixed string committed to
# this public repo, so anyone who's read the source knows it. If it were
# ever live, they could forge Django's signed session cookies (instant
# staff/admin impersonation on any tenant), forge CSRF tokens, AND forge
# orders.tokens' ticket-QR HMAC signatures (derived straight from
# SECRET_KEY). Re-declare it here with NO insecure fallback and reject the
# known default / an obviously-too-short value, so a prod deploy that
# forgets to set SECRET_KEY fails loudly at startup instead of silently
# booting with a key an attacker already has.
SECRET_KEY = env("SECRET_KEY", default="")
if not SECRET_KEY or SECRET_KEY == "insecure-dev-key-do-not-use-in-prod" or len(SECRET_KEY) < 20:
    raise ImproperlyConfigured(
        "SECRET_KEY must be set to a long, random, unique value in production "
        "(set the SECRET_KEY environment variable -- see .env.example). Refusing "
        "to start with a missing/default/weak key."
    )

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[".boxo.show"])
# Tenant subdomains all live under BASE_DOMAIN; trust them for CSRF.
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS", default=["https://*.boxo.show", "https://boxo.show"]
)

# SQLite lives in the app dir's data/ (created by lab980 provision-site). Ensure
# it exists so a fresh deploy migrates cleanly. Set DATABASE_URL for Postgres.
os.makedirs(BASE_DIR / "data", exist_ok=True)
DATABASES = harden_sqlite({
    "default": env.db(
        "DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'data' / 'db.sqlite3'}"
    )
})

# Shared cache across gunicorn workers. A file-based cache in the app dir is
# the right fit for the single-droplet deploy: no extra infra (unlike Redis/
# Memcached), yet shared across all workers on the host -- which the login
# throttle (accounts/throttle.py) needs to actually hold, since the default
# per-process LocMemCache would give each worker its own counter. Postgres/
# Redis deployments can override CACHES via their own settings if desired.
os.makedirs(BASE_DIR / "data" / "cache", exist_ok=True)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": str(BASE_DIR / "data" / "cache"),
    }
}

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# nginx terminates TLS and forwards X-Forwarded-Proto (lab980 vhost); this lets
# Django know the original request was https.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS: every tenant is served over TLS only (SECURE_SSL_REDIRECT above), so
# tell browsers to skip plaintext http:// entirely for a year, on every
# tenant subdomain (includeSubDomains matters here specifically: browsers
# key HSTS per-host, and new tenant subdomains are added over time), and opt
# into browser preload lists. `check --deploy` flagged this as unset
# (security.W004) before this block was added.
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=63072000)  # 2 years
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# X-Content-Type-Options: nosniff. Django's own default is already True
# (SecurityMiddleware), set explicitly so it's visible here rather than
# implicit.
SECURE_CONTENT_TYPE_NOSNIFF = True

# Referrer-Policy. Django's default ("same-origin") is already reasonable;
# pinned explicitly to the same modern-browser-recommended value so it isn't
# left to a framework default that could change.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@boxo.show")
