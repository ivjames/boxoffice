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
    "guests",
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
    # Last: stamps `no-store` on HTML responses so Safari can't serve a stale
    # page on refresh (static assets stay hash-cached). See the middleware's
    # docstring. Innermost, so it wraps only real view responses -- WhiteNoise's
    # static responses short-circuit above it.
    "config.middleware.NoCacheHTMLMiddleware",
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
                # Exposes the current staff Membership (or None) as `staff_membership`.
                "accounts.context_processors.staff_membership",
                # Exposes the signed-in guest ticket-buyer (or None) as `guest_account`.
                "guests.context_processors.guest_account",
                # Exposes the session's live cart item count as `cart_count`.
                "orders.context_processors.cart_count",
                # Exposes settings.ENABLE_TEST_CHECKOUT as `test_checkout_enabled`.
                "payments.context_processors.test_checkout_enabled",
                # Exposes the resolved deploy stamp as `app_version` (footer).
                "config.context_processors.app_version",
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
# "beta" is reserved by default because beta.<BASE_DOMAIN> is the staging
# deployment's host (a separate app instance, see DEPLOY.md "Beta / staging
# site") — reserving it here keeps a tenant from ever claiming that label.
RESERVED_SUBDOMAINS = set(
    env.list("RESERVED_SUBDOMAINS", default=["www", "app", "admin", "beta"])
)

# The base domain tenants live under, e.g. "boxo.show" so that
# "roxy.boxo.show" resolves to the "roxy" tenant. TenantMiddleware strips
# this suffix off the Host header to find the subdomain.
BASE_DOMAIN = env("BASE_DOMAIN", default="localhost")

# --- TEST CHECKOUT (env-gated fake-payment path) -------------------------
# When True, orders/views.py's checkout_test view (and the "Pay (TEST -- no
# real charge)" button it powers, see templates/orders/checkout.html +
# base.html's TEST MODE banner) becomes reachable: it fulfills a Hold via
# payments.services.fulfill_hold() with provider="test" and a synthetic
# payment_ref, WITHOUT ever calling Stripe or requiring any Stripe keys.
# Real tickets get created for zero payment.
#
# Default False, and MUST STAY False in any real production deployment --
# this exists purely so an operator with no Stripe account yet can exercise
# the full browse -> buy -> ticket -> scan flow. When False, checkout_test
# 404s unconditionally (checked per-request, not at urlconf import time, so
# it responds correctly to test overrides too) and the storefront only ever
# shows the normal Stripe checkout button. See .env.example for the loud
# warning next to ENABLE_TEST_CHECKOUT.
ENABLE_TEST_CHECKOUT = env.bool("ENABLE_TEST_CHECKOUT", default=False)
