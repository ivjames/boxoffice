from django.conf import settings


def test_checkout_enabled(request):
    """Exposes settings.ENABLE_TEST_CHECKOUT to every template as
    `test_checkout_enabled` -- both base.html (the loud TEST MODE banner)
    and orders/checkout.html (the "Pay (TEST -- no real charge)" button)
    need it globally, not just on one view. False unless the operator has
    explicitly set ENABLE_TEST_CHECKOUT=true in .env; see that setting's
    docstring in config/settings/base.py for why it must never be on in
    real production.
    """
    return {"test_checkout_enabled": settings.ENABLE_TEST_CHECKOUT}
