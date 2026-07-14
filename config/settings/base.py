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
    "promotions",
    "donations",
    "passes",
    "campaigns",
    "orders",
    "payments",
    "dashboard",
    "scanning",
    "helpcenter",
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
                # Exposes whether donations are switched on for this tenant as
                # `donations_enabled` (nav link, cart add-on gating).
                "donations.context_processors.donation_nav",
                # Exposes whether passes are switched on for this tenant as
                # `passes_enabled` (nav link), and the session's in-progress
                # redemption as `redeeming_pass` (redeem-mode banner, cart/
                # checkout "Redeem with pass" gating).
                "passes.context_processors.pass_nav",
                # Exposes settings.ENABLE_TEST_CHECKOUT as `test_checkout_enabled`.
                "payments.context_processors.test_checkout_enabled",
                # Exposes the resolved deploy stamp as `app_version` (footer).
                "config.context_processors.app_version",
                # Exposes settings.SHOW_ADMIN_LINK as `show_admin_link` (nav/footer).
                "config.context_processors.show_admin_link",
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

# --- Stripe Connect (platform account) -----------------------------------
# boxo.show is the platform Stripe account; each theater is a connected
# (Express) account onboarded through it. Unlike the old bring-your-own-key
# model, these are PLATFORM-wide credentials set once here (via env), not
# per-tenant rows: the one secret key authenticates every call (with
# `stripe_account=<acct_id>` selecting the theater for a direct charge), and
# the one webhook secret verifies the single Connect endpoint that receives
# every theater's events. See payments/services.py + payments/views.py.
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

# Platform take rate applied to each order as a Stripe application fee on the
# direct charge (payments.services.application_fee_amount): PLATFORM_FEE_PERCENT
# percent of the order total plus PLATFORM_FEE_FIXED_CENTS flat. BOTH DEFAULT
# TO 0 -- i.e. no cut -- which is deliberate: the fee MECHANISM ships now, but
# the actual rate is set later, per the launch plan. A per-theater override
# lives on Organization.platform_fee_percent. With a 0 rate no application fee
# is sent at all (Stripe rejects an explicit fee of 0).
PLATFORM_FEE_PERCENT = env.int("PLATFORM_FEE_PERCENT", default=0)
PLATFORM_FEE_FIXED_CENTS = env.int("PLATFORM_FEE_FIXED_CENTS", default=0)

# --- Campaign email sender (Phase 4) -------------------------------------
# Max CampaignSend rows the cron batch sender
# (campaigns.management.commands.send_campaign_emails) drains per run. Caps how
# many SMTP round-trips one tick makes so a large blast paces out across ticks
# rather than blocking a single run -- the same one-thing-per-tick discipline
# the Hold sweeper / tenant provisioner use. Tune up on a fast transactional
# provider, down on a rate-limited SMTP relay.
CAMPAIGN_BATCH_SIZE = env.int("CAMPAIGN_BATCH_SIZE", default=50)

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

# --- Login throttling ----------------------------------------------------
# Cache-backed rate limit on the staff login and the guest magic-link request
# (accounts/throttle.py), to blunt password-guessing and email-bombing. After
# LOGIN_RATELIMIT_MAX_ATTEMPTS failures from one IP within
# LOGIN_RATELIMIT_WINDOW_SECONDS, further attempts are refused until the
# window rolls off. Effectiveness depends on a SHARED cache across gunicorn
# workers -- prod configures a file-based cache in the app dir for exactly
# this reason (dev/test use the default per-process LocMemCache, which is
# fine there). Set MAX_ATTEMPTS to 0 to disable entirely.
LOGIN_RATELIMIT_MAX_ATTEMPTS = env.int("LOGIN_RATELIMIT_MAX_ATTEMPTS", default=10)
LOGIN_RATELIMIT_WINDOW_SECONDS = env.int("LOGIN_RATELIMIT_WINDOW_SECONDS", default=900)

# Surface a convenience "Admin" link (-> /admin/) in the platform-host nav and
# footer. Default False: the public marketing landing page (prod) deliberately
# does not advertise the superuser surface. The staging/beta deploy sets
# SHOW_ADMIN_LINK=true so operators can reach the admin from its landing page.
# /admin/ itself is always reachable directly regardless of this flag.
SHOW_ADMIN_LINK = env.bool("SHOW_ADMIN_LINK", default=False)
