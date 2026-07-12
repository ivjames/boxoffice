"""config.observability.init_sentry must be a safe no-op when no DSN is set
(BO-8): every dev/test run and any prod deploy that hasn't configured Sentry
must not initialize the SDK, make a network call, or error."""

from django.test import SimpleTestCase

from config.observability import init_sentry


class InitSentryTests(SimpleTestCase):
    def test_no_dsn_is_a_noop(self):
        self.assertFalse(init_sentry(""))
        self.assertFalse(init_sentry(None))
