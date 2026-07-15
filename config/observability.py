"""Optional error monitoring (BO-8).

Sentry is initialized only when a SENTRY_DSN is configured; with none set (the
default, and every dev/test run), this is a silent no-op -- no network calls,
no import requirement. Kept out of settings modules so it's unit-testable
without importing prod settings.
"""


def init_sentry(dsn, *, environment="production", traces_sample_rate=0.0):
    """Initialize Sentry if `dsn` is set. Returns True when Sentry was
    initialized, False otherwise (no DSN, or sentry-sdk not installed). Safe
    to call unconditionally: a missing DSN or missing package must never stop
    the app booting over optional observability."""
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
    except ImportError:
        return False
    sentry_sdk.init(
        dsn=dsn,
        integrations=[DjangoIntegration()],
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        # Buyer emails/names flow through orders; don't ship PII to Sentry by
        # default. Flip deliberately if you need it for debugging.
        send_default_pii=False,
    )
    return True
