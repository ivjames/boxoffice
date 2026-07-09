"""Phase 9 (security hardening) regression tests for config/settings/prod.py.

The core bug: config/settings/base.py's SECRET_KEY has a default
("insecure-dev-key-do-not-use-in-prod") so a bare clone works with zero
setup in dev. Before this fix, config/settings/prod.py did `from .base
import *` and never re-declared SECRET_KEY, so a prod deploy that forgot to
set the SECRET_KEY env var would silently boot with that exact fixed
string -- which is committed to this public repo, so anyone who's read the
source already knows it. .env.example already claimed "REQUIRED in prod
(config/settings/prod.py has no insecure fallback there)" -- that comment
was aspirational; the code didn't actually enforce it. A known SECRET_KEY
undermines every one of Django's SECRET_KEY-derived cryptographic
guarantees platform-wide (CSRF masking, the messages framework's cookie
signing, session auth-hash invalidation on password change) and, in this
app specifically, orders/tokens.py's ticket-QR HMAC scheme, which documents
itself as deriving its per-tenant signing key directly from
settings.SECRET_KEY. The exact blast radius depends on which of those a
given attacker can reach, but shipping with a key the public already knows
is an unconditional violation of Django's own security guidance
(security.W009) and of this repo's own stated intent.

These tests exercise the REAL config/settings/prod.py module by spawning a
fresh interpreter (subprocess), the same way
orders/test_concurrency_multiprocess.py spawns fresh interpreters for real
multiprocess DB concurrency -- settings modules only run their top-level
code once per process, and this test suite already has config.settings.dev
loaded, so there's no clean way to re-exercise prod.py's import-time
ImproperlyConfigured checks in-process.
"""

import subprocess
import sys
from pathlib import Path

from django.test import TestCase

BASE_DIR = Path(__file__).resolve().parent.parent


def _run_prod_settings(secret_key_env):
    """Boot config.settings.prod in a fresh interpreter with SECRET_KEY set
    (or omitted) as given. Returns the completed subprocess (returncode +
    stderr) without ever touching this test process's own already-configured
    Django settings."""
    env = {
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "ALLOWED_HOSTS": "example.com",
        "PATH": "/usr/bin:/bin",
    }
    if secret_key_env is not None:
        env["SECRET_KEY"] = secret_key_env
    return subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class ProdSettingsRefuseWeakSecretKeyTests(TestCase):
    """config/settings/prod.py must fail fast at import time rather than
    ever silently running with base.py's dev-only fallback key."""

    def test_missing_secret_key_refuses_to_boot(self):
        result = _run_prod_settings(secret_key_env=None)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("SECRET_KEY", result.stderr)

    def test_known_insecure_default_key_refuses_to_boot(self):
        """Even if SECRET_KEY is explicitly exported as base.py's literal
        dev-default string (e.g. a copy-pasted .env, or an old deploy
        script), prod must still refuse -- that value is public."""
        result = _run_prod_settings(secret_key_env="insecure-dev-key-do-not-use-in-prod")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("ImproperlyConfigured", result.stderr)

    def test_short_secret_key_refuses_to_boot(self):
        result = _run_prod_settings(secret_key_env="tiny")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("ImproperlyConfigured", result.stderr)

    def test_strong_secret_key_boots_cleanly(self):
        result = _run_prod_settings(
            secret_key_env="a" * 64  # long/random enough to pass the length check
        )
        self.assertEqual(result.returncode, 0, result.stderr)
