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

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, env, harden_sqlite

DEBUG = False

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[".lab980.com"])
# Tenant subdomains all live under BASE_DOMAIN; trust them for CSRF.
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS", default=["https://*.lab980.com", "https://lab980.com"]
)

# SQLite lives in the app dir's data/ (created by lab980 provision-site). Ensure
# it exists so a fresh deploy migrates cleanly. Set DATABASE_URL for Postgres.
os.makedirs(BASE_DIR / "data", exist_ok=True)
DATABASES = harden_sqlite({
    "default": env.db(
        "DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'data' / 'db.sqlite3'}"
    )
})

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# nginx terminates TLS and forwards X-Forwarded-Proto (lab980 vhost); this lets
# Django know the original request was https.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@lab980.com")
