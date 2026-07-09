"""
Local development settings. Zero-setup by default: no Postgres, no .env
required — just `pip install`, `migrate`, `runserver`.
"""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, env, harden_sqlite

DEBUG = env.bool("DEBUG", default=True)

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

# DATABASE_URL fallback: if it's not set in .env/the environment, fall back to
# a local SQLite file so a fresh clone can `migrate` + `runserver` with zero
# external setup. Set DATABASE_URL (e.g. to a local Postgres) to opt into
# parity with prod when testing Postgres-specific behavior (e.g. row locking
# via select_for_update, used heavily by the seat-holding logic in later
# phases — SQLite's locking semantics differ, so do that testing against
# Postgres even in dev).
DATABASES = harden_sqlite({
    "default": env.db(
        "DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
    )
})

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
