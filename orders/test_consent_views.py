"""Buyer marketing-consent view coverage (Phase 4 CRM UI layer).

Owned by the UI-layer agent (not the concurrent Phase 4 test agent, who owns
campaigns/tests.py, guests/test_marketing.py, dashboard/test_campaigns.py,
dashboard/test_audience.py, and the payments test extensions -- this file
stays entirely out of those). Covers the buyer-facing half of the consent
plumbing: the `marketing_opt_in` checkbox threading through checkout_stub,
checkout_test, and the real (mocked) Stripe checkout_view POST, all the way
to guests.services.record_marketing_opt_in actually flipping the flag.

Reuses orders.test_views' fixtures (TenantClientMixin, StorefrontFixtureMixin)
so the storefront shape is built exactly the way the rest of the suite builds
it -- same convention passes/test_views.py and guests/tests.py follow.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from guests.models import GuestAccount
from orders.models import Hold
from orders.test_views import StorefrontFixtureMixin, TenantClientMixin


class StubCheckoutConsentTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)

    def _hold(self, quantity=1):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": quantity},
        )
        return Hold.objects.get(performance=self.performance)

    def test_checked_box_opts_the_guest_in(self):
        hold = self._hold()
        self.post_as(
            "org-a",
            "/checkout/stub/",
            {
                "hold_id": hold.pk,
                "buyer_name": "Stub Buyer",
                "buyer_email": "stub@example.com",
                "marketing_opt_in": "on",
            },
        )
        guest = GuestAccount.objects.get(email="stub@example.com")
        self.assertTrue(guest.marketing_opt_in)
        self.assertIsNotNone(guest.marketing_opt_in_at)

    def test_unchecked_box_leaves_guest_opted_out(self):
        hold = self._hold()
        self.post_as(
            "org-a",
            "/checkout/stub/",
            {"hold_id": hold.pk, "buyer_name": "Stub Buyer", "buyer_email": "stub2@example.com"},
        )
        guest = GuestAccount.objects.get(email="stub2@example.com")
        self.assertFalse(guest.marketing_opt_in)
        self.assertIsNone(guest.marketing_opt_in_at)

    @override_settings(ENABLE_TEST_CHECKOUT=True)
    def test_checkout_test_checked_box_opts_the_guest_in(self):
        """The env-gated /checkout/test/ fake-payment path threads the same
        checkbox through fulfill_hold -- ENABLE_TEST_CHECKOUT is off by
        default (config.settings.base) and turned on per-test, mirroring
        guests.tests.GuestPortalTests' own use of this endpoint."""
        hold = self._hold()
        self.post_as(
            "org-a",
            "/checkout/test/",
            {"hold_id": hold.pk, "buyer_email": "tester@example.com", "marketing_opt_in": "1"},
        )
        guest = GuestAccount.objects.get(email="tester@example.com")
        self.assertTrue(guest.marketing_opt_in)

    def test_repeat_purchase_without_reticking_never_opts_back_out(self):
        """record_marketing_opt_in is one-way (guests.services docstring): a
        second purchase that leaves the box unticked must not silently
        un-subscribe an already-opted-in guest."""
        hold = self._hold()
        self.post_as(
            "org-a",
            "/checkout/stub/",
            {
                "hold_id": hold.pk,
                "buyer_email": "loyal@example.com",
                "marketing_opt_in": "on",
            },
        )
        guest = GuestAccount.objects.get(email="loyal@example.com")
        self.assertTrue(guest.marketing_opt_in)

        hold2 = self._hold()
        self.post_as(
            "org-a",
            "/checkout/stub/",
            {"hold_id": hold2.pk, "buyer_email": "loyal@example.com"},
        )
        guest.refresh_from_db()
        self.assertTrue(guest.marketing_opt_in)


class RealStripeCheckoutConsentTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """The real (mocked -- no network) Stripe path: consent has to ride
    Stripe metadata since fulfillment happens out-of-band on the webhook, not
    on this request (see payments.services.create_checkout_session)."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)
        self.org.stripe_account_id = "acct_org_a"
        self.org.stripe_charges_enabled = True
        self.org.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])

    def _hold(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
        )
        return Hold.objects.get(performance=self.performance)

    def test_checked_box_carries_consent_in_stripe_metadata(self):
        hold = self._hold()
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = type(
                "FakeSession", (), {"url": "https://checkout.stripe.com/pay/cs_test_123"}
            )()
            self.post_as("org-a", "/checkout/", {"hold_id": hold.pk, "marketing_opt_in": "on"})
        self.assertEqual(mock_create.call_args.kwargs["metadata"].get("marketing_opt_in"), "1")

    def test_unchecked_box_omits_consent_from_stripe_metadata(self):
        hold = self._hold()
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = type(
                "FakeSession", (), {"url": "https://checkout.stripe.com/pay/cs_test_123"}
            )()
            self.post_as("org-a", "/checkout/", {"hold_id": hold.pk})
        self.assertNotIn("marketing_opt_in", mock_create.call_args.kwargs["metadata"])
