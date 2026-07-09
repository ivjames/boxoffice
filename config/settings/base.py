"""
Settings shared by every environment. Environment-specific overrides live in
dev.py / prod.py, which both `from .base import *` and then adjust.
"""

from pathlib import Path

import environ

# BASE_DIR = repo root (config/settings/base.py -> config/settings -> config -> repo root)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# .env lives at the repo root, next to manage.py. Safe to call even if the file
# is missing (e.g. prod, where real env vars are set by pm2/systemd instead).
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-do-not-use-in-prod")


def harden_sqlite(databases):
    """Make SQLite safe for concurrent booking under the lab980 single-droplet
    deploy model. For a SQLite `default`, acquire the write lock at BEGIN
    (transaction_mode=IMMEDIATE) so two simultaneous checkouts serialize instead
    of racing, and wait rather than error on contention (timeout). Combined with
    every booking mutation running inside transaction.atomic() + re-checking
    availability, this makes seat/GA double-booking impossible. No-op for
    Postgres, where select_for_update() does the real row locking. Requires
    Django 5.1+ for the transaction_mode option.
    """
    default = databases.get("default", {})
    if default.get("ENGINE", "").endswith("sqlite3"):
        default.setdefault("OPTIONS", {}).update(
            {"timeout": 20, "transaction_mode": "IMMEDIATE"}
        )
    return databases

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "tenants",
    "accounts",
    "venues",
    "events",
    "orders",
    "payments",
    "dashboard",
    "scanning",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves static files directly from the app (no nginx location block), so
    # the lab980 vhost stays a plain proxy-to-port like every other site.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Must come after auth (request.user) but resolve early enough that views
    # and templates can rely on request.organization being set.
    "tenants.middleware.TenantMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Exposes request.organization to every template as `organization`.
                "tenants.context_processors.organization",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "accounts.User"

# Staff sign-in is per-tenant (accounts.views.login_view) -- this is a URL
# NAME, not a path, so it resolves correctly on whichever subdomain a
# redirect-to-login happens on (every tenant subdomain serves the same
# urlconf; only request.organization differs).
LOGIN_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # WhiteNoise: hashed filenames + gzip/brotli, served by the app.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Multi-tenancy -----------------------------------------------------
# Subdomains that map to the platform host (marketing/landing, signup, admin)
# rather than a specific tenant. An empty/missing Host subdomain (e.g. bare
# BASE_DOMAIN, or a host that doesn't end in BASE_DOMAIN at all — like an IP
# or localhost during early dev) is always treated as reserved too.
RESERVED_SUBDOMAINS = set(
    env.list("RESERVED_SUBDOMAINS", default=["www", "app", "admin"])
)

# The base domain tenants live under, e.g. "lab980.com" so that
# "roxy.lab980.com" resolves to the "roxy" tenant. TenantMiddleware strips
# this suffix off the Host header to find the subdomain.
BASE_DOMAIN = env("BASE_DOMAIN", default="localhost")
