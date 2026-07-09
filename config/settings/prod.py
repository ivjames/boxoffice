"""
Production settings. Postgres is required (no SQLite fallback) — DATABASE_URL
must be set or startup fails loudly rather than silently writing to a local
SQLite file that isn't backed up, isn't shared across processes, and can't
handle the row-level locking Phase 3's seat/GA holds rely on.
"""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

# No default here: raises django.core.exceptions.ImproperlyConfigured if
# DATABASE_URL is unset, e.g. "postgres://user:pass@host:5432/boxoffice".
DATABASES = {"default": env.db("DATABASE_URL")}

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@lab980.com")
