"""HTTP-layer tests for the passes UI (passes/views.py): the storefront
(list/detail/stub buy flow), the redemption flow (redeem_start/redeem_exit/
redeem against a cart hold), and tenant/ownership isolation. The money-path
logic itself (fulfill_pass_purchase, fulfill_hold_with_pass,
restore_redemptions_for_order) is covered by the other test agent in
passes/test_services.py and payments/test_services.py -- these tests only
exercise the views: scoping, session state, redirects, and that the
templates render without 500ing.

Setup style mirrors guests/tests.py: reuse orders.test_views' fixtures
(TenantClientMixin, StorefrontFixtureMixin) so the storefront shape (org, GA
performance, tier) is built exactly the way the rest of the suite builds it.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from guests.models import GuestAccount
from orders.models import Hold, Order, Ticket
from orders.test_views import StorefrontFixtureMixin, TenantClientMixin
from promotions.models import PromoCode

from .models import PassProduct, PassPurchase


class PassesFixtureMixin(StorefrontFixtureMixin):
    def make_product(self, org, kind=PassProduct.Kind.FLEX, **kwargs):
        defaults = dict(
            organization=org,
            name="Flex 3-Pack",
            kind=kind,
            price=Decimal("45.00"),
            is_active=True,
        )
        if kind == PassProduct.Kind.FLEX:
            defaults["credit_count"] = kwargs.pop("credit_count", 2)
        defaults.update(kwargs)
        return PassProduct.objects.create(**defaults)


class PassListViewTests(TenantClientMixin, PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")

    def test_no_active_products_404s(self):
        resp = self.get_as("org-a", "/passes/")
        self.assertEqual(resp.status_code, 404)

    def test_lists_active_products_only(self):
        active = self.make_product(self.org, name="Active Pass")
        inactive = self.make_product(self.org, name="Inactive Pass", is_active=False)
        resp = self.get_as("org-a", "/passes/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Active Pass")
        self.assertNotContains(resp, "Inactive Pass")

    def test_tenant_isolation(self):
        self.make_product(self.org, name="Mine")
        other_org, _other_venue = self.build_org("org-b")
        self.make_product(other_org, name="Theirs")
        resp = self.get_as("org-a", "/passes/")
        self.assertContains(resp, "Mine")
        self.assertNotContains(resp, "Theirs")


class PassDetailViewTests(TenantClientMixin, PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.product = self.make_product(self.org)

    def test_get_renders_buy_form(self):
        resp = self.get_as("org-a", f"/passes/{self.product.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.product.name)
        self.assertContains(resp, "Buy for $45.00")

    def test_inactive_product_404s(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])
        resp = self.get_as("org-a", f"/passes/{self.product.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_cross_org_product_404s(self):
        other_org, _other_venue = self.build_org("org-b")
        other_product = self.make_product(other_org, name="Theirs")
        resp = self.get_as("org-a", f"/passes/{other_product.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_post_without_email_shows_error(self):
        resp = self.post_as("org-a", f"/passes/{self.product.pk}/", {"buyer_name": "Jo"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter an email")
        self.assertEqual(PassPurchase.objects.count(), 0)

    def test_post_not_stripe_connected_redirects_to_stub(self):
        # This org hasn't finished Connect onboarding (the default state) --
        # create_pass_checkout_session's STUB MODE redirects here instead of
        # calling Stripe.
        resp = self.post_as(
            "org-a",
            f"/passes/{self.product.pk}/",
            {"buyer_email": "buyer@example.com", "buyer_name": "Buyer"},
        )
        self.assertRedirects(
            resp,
            f"http://org-a.localhost/passes/stub/?product_id={self.product.pk}",
            fetch_redirect_response=False,
        )
        self.assertEqual(PassPurchase.objects.count(), 0)

    def test_post_stripe_connected_redirects_to_checkout_session(self):
        from unittest.mock import patch

        self.org.stripe_account_id = "acct_org_a"
        self.org.stripe_charges_enabled = True
        self.org.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = type(
                "FakeSession", (), {"url": "https://checkout.stripe.com/pay/cs_test_pass"}
            )()
            resp = self.post_as(
                "org-a",
                f"/passes/{self.product.pk}/",
                {"buyer_email": "buyer@example.com", "buyer_name": "Buyer"},
            )
        self.assertRedirects(
            resp, "https://checkout.stripe.com/pay/cs_test_pass", fetch_redirect_response=False
        )
        self.assertEqual(PassPurchase.objects.count(), 0)


class PassStubBuyFlowTests(TenantClientMixin, PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.product = self.make_product(self.org, credit_count=3, price=Decimal("60.00"))

    def test_get_renders_product_summary(self):
        resp = self.get_as("org-a", f"/passes/stub/?product_id={self.product.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.product.name)
        self.assertContains(resp, "$60.00")

    def test_unknown_product_id_404s(self):
        resp = self.get_as("org-a", "/passes/stub/?product_id=999999")
        self.assertEqual(resp.status_code, 404)

    def test_stub_unavailable_once_charges_enabled(self):
        self.org.stripe_charges_enabled = True
        self.org.save(update_fields=["stripe_charges_enabled"])
        resp = self.get_as("org-a", f"/passes/stub/?product_id={self.product.pk}")
        self.assertEqual(resp.status_code, 404)

    def test_post_creates_purchase_signs_in_and_shows_receipt(self):
        resp = self.post_as(
            "org-a",
            "/passes/stub/",
            {
                "product_id": self.product.pk,
                "buyer_email": "buyer@example.com",
                "buyer_name": "Buyer",
            },
        )
        purchase = PassPurchase.objects.get()
        self.assertEqual(purchase.product_id, self.product.pk)
        self.assertEqual(purchase.kind, PassProduct.Kind.FLEX)
        self.assertEqual(purchase.credit_count, 3)
        self.assertEqual(purchase.credits_remaining, 3)
        self.assertEqual(purchase.status, PassPurchase.Status.ACTIVE)

        order = Order.objects.get()
        self.assertEqual(order.total, Decimal("60.00"))
        self.assertIsNone(order.performance)
        self.assertEqual(purchase.order_id, order.pk)

        # Signed in on this session.
        guest = GuestAccount.objects.get(email="buyer@example.com")
        self.assertEqual(self.client.session.get("guest_account_id"), guest.pk)

        self.assertRedirects(resp, f"/tickets/{order.token}/", fetch_redirect_response=False)
        receipt = self.get_as("org-a", f"/tickets/{order.token}/")
        self.assertContains(receipt, "Your pass")
        self.assertContains(receipt, self.product.name)
        self.assertContains(receipt, "3 credits")
        self.assertContains(receipt, "View in my account")

    def test_post_without_email_shows_error_and_stays_on_stub(self):
        resp = self.post_as(
            "org-a", "/passes/stub/", {"product_id": self.product.pk, "buyer_name": "Buyer"}
        )
        self.assertRedirects(
            resp, f"/passes/stub/?product_id={self.product.pk}", fetch_redirect_response=False
        )
        self.assertEqual(PassPurchase.objects.count(), 0)


class SeasonPassReceiptTests(TenantClientMixin, PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.product = self.make_product(
            self.org, kind=PassProduct.Kind.SEASON, name="Season Pass", price=Decimal("120.00")
        )

    def test_season_purchase_and_receipt(self):
        self.post_as(
            "org-a",
            "/passes/stub/",
            {"product_id": self.product.pk, "buyer_email": "season@example.com"},
        )
        purchase = PassPurchase.objects.get()
        self.assertEqual(purchase.kind, PassProduct.Kind.SEASON)
        self.assertIsNone(purchase.credit_count)
        self.assertIsNone(purchase.credits_remaining)

        order = Order.objects.get()
        receipt = self.get_as("org-a", f"/tickets/{order.token}/")
        self.assertContains(receipt, "One admission per covered show")


class PassRedemptionFlowTests(TenantClientMixin, PassesFixtureMixin, TestCase):
    """The full buy -> sign in -> redeem_start -> hold -> redeem loop, plus
    every PassRedemptionError path bouncing back to the cart with a flashed,
    buyer-safe message."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=10)
        self.other_event, self.other_performance, self.other_tier = self.build_ga(
            self.org, self.venue, slug="other-show", capacity=10
        )

    def _buy_flex(self, credit_count=2, price=Decimal("50.00"), events=None):
        product = self.make_product(self.org, credit_count=credit_count, price=price)
        if events is not None:
            product.events.set(events)
        self.post_as(
            "org-a",
            "/passes/stub/",
            {"product_id": product.pk, "buyer_email": "holder@example.com"},
        )
        return PassPurchase.objects.get(product=product)

    def _hold_for(self, performance, tier, quantity=1):
        self.post_as(
            "org-a",
            f"/performances/{performance.pk}/hold/",
            {"price_tier": tier.pk, "quantity": quantity},
        )
        return Hold.objects.filter(performance=performance).latest("created_at")

    def test_redeem_start_requires_signed_in_guest(self):
        product = self.make_product(self.org)
        # Nobody signed in on this session.
        resp = self.post_as("org-a", "/passes/redeem/start/", {"pass_id": product.pk})
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.assertNotIn("redeeming_pass_id", self.client.session)

    def test_redeem_start_requires_ownership(self):
        purchase = self._buy_flex()
        # A second guest, signed in on the SAME session (simulating someone
        # else's browser), must not be able to load holder@example.com's pass.
        other_guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, "someone@else.com")
        session = self.client.session
        session["guest_account_id"] = other_guest.pk
        session["guest_org_id"] = self.org.pk
        session.save()

        resp = self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        self.assertEqual(resp.status_code, 404)

    def test_full_redeem_flow_decrements_credits_and_pops_session(self):
        purchase = self._buy_flex(credit_count=2)
        start_resp = self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        self.assertRedirects(start_resp, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session.get("redeeming_pass_id"), purchase.pk)

        hold = self._hold_for(self.performance, self.tier, quantity=1)
        resp = self.post_as("org-a", "/passes/redeem/", {"hold_id": hold.pk})

        redemption_order = Order.objects.exclude(pk=purchase.order_id).get()
        self.assertEqual(redemption_order.total, Decimal("0.00"))
        self.assertEqual(redemption_order.performance_id, self.performance.pk)
        self.assertEqual(Ticket.objects.filter(order=redemption_order).count(), 1)
        self.assertRedirects(
            resp, f"/tickets/{redemption_order.token}/", fetch_redirect_response=False
        )

        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 1)
        self.assertEqual(purchase.status, PassPurchase.Status.ACTIVE)

        # Session key popped -- no longer in redeem mode.
        self.assertNotIn("redeeming_pass_id", self.client.session)
        self.assertFalse(Hold.objects.filter(pk=hold.pk).exists())

    def test_exhausted_pass_cannot_re_enter_redeem_mode(self):
        purchase = self._buy_flex(credit_count=1)
        self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        hold = self._hold_for(self.performance, self.tier, quantity=1)
        self.post_as("org-a", "/passes/redeem/", {"hold_id": hold.pk})

        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PassPurchase.Status.EXHAUSTED)

        resp = self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk}, follow=True)
        self.assertRedirects(resp, "/account/")
        self.assertContains(resp, "can&#x27;t be redeemed right now")
        self.assertNotIn("redeeming_pass_id", self.client.session)

    def test_uncovered_event_flashes_error_and_stays_on_cart(self):
        # Pass only covers self.event -- redeeming against other_event's hold
        # must be rejected by fulfill_hold_with_pass and flashed here.
        purchase = self._buy_flex(events=[self.event])
        self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        hold = self._hold_for(self.other_performance, self.other_tier, quantity=1)

        resp = self.post_as("org-a", "/passes/redeem/", {"hold_id": hold.pk}, follow=True)
        self.assertRedirects(resp, "/cart/")
        self.assertContains(resp, "doesn&#x27;t cover this performance")
        # Nothing was consumed -- the hold and the pass are untouched.
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 2)

    def test_promo_carrying_hold_is_rejected(self):
        purchase = self._buy_flex()
        self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        hold = self._hold_for(self.performance, self.tier, quantity=1)
        PromoCode.objects.create(
            organization=self.org, code="SAVE", kind=PromoCode.Kind.PERCENT, value=Decimal("10")
        )
        self.post_as("org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "SAVE"})

        resp = self.post_as("org-a", "/passes/redeem/", {"hold_id": hold.pk}, follow=True)
        self.assertRedirects(resp, "/cart/")
        self.assertContains(resp, "Remove the promo code")

    def test_redeem_without_active_redemption_session_flashes_and_pops(self):
        purchase = self._buy_flex()
        # Sign in without ever calling redeem_start.
        hold = self._hold_for(self.performance, self.tier, quantity=1)
        resp = self.post_as("org-a", "/passes/redeem/", {"hold_id": hold.pk}, follow=True)
        self.assertRedirects(resp, "/cart/")
        self.assertContains(resp, "expired")
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 2)

    def test_redeem_exit_pops_session(self):
        purchase = self._buy_flex()
        self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        self.assertIn("redeeming_pass_id", self.client.session)
        resp = self.post_as("org-a", "/passes/redeem/exit/")
        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        self.assertNotIn("redeeming_pass_id", self.client.session)

    def test_cross_org_pass_id_404s_at_redeem_start(self):
        purchase = self._buy_flex()
        other_org, other_venue = self.build_org("org-b")
        resp = self.post_as("org-b", "/passes/redeem/start/", {"pass_id": purchase.pk})
        # No guest signed in on org-b's session at all -> bounced to sign in,
        # never even reaches the org-scoped lookup. Sign in a org-b guest to
        # exercise the org-scoping 404 specifically.
        self.assertEqual(resp.status_code, 302)

        guest_b, _ = GuestAccount.objects.get_or_create_for_email(other_org, "holder@example.com")
        session = self.client.session
        session["guest_account_id"] = guest_b.pk
        session["guest_org_id"] = other_org.pk
        session.save()
        resp = self.post_as("org-b", "/passes/redeem/start/", {"pass_id": purchase.pk})
        self.assertEqual(resp.status_code, 404)


class RedeemModeUITests(TenantClientMixin, PassesFixtureMixin, TestCase):
    """The redeem-mode banner (templates/base.html) and the cart's
    "Redeem with pass" CTA / "not covered" note (templates/orders/cart.html),
    gated by passes.context_processors.pass_nav + passes/templatetags/
    pass_tags.py."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=10)
        self.other_event, self.other_performance, self.other_tier = self.build_ga(
            self.org, self.venue, slug="other-show", capacity=10
        )

    def _redeeming_purchase(self, events=None, credit_count=2):
        product = self.make_product(self.org, credit_count=credit_count)
        if events is not None:
            product.events.set(events)
        self.post_as(
            "org-a", "/passes/stub/", {"product_id": product.pk, "buyer_email": "holder@example.com"}
        )
        purchase = PassPurchase.objects.get(product=product)
        self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        return purchase

    def test_no_banner_when_not_redeeming(self):
        resp = self.get_as("org-a", "/cart/")
        self.assertNotContains(resp, "redeem-banner")
        self.assertNotContains(resp, "Redeeming with")

    def test_banner_shown_when_redeeming(self):
        purchase = self._redeeming_purchase()
        resp = self.get_as("org-a", "/cart/")
        self.assertContains(resp, "Redeeming with")
        self.assertContains(resp, "2 credits remaining")

    def test_covered_hold_shows_redeem_cta_and_hides_promo(self):
        self._redeeming_purchase()
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
        )
        resp = self.get_as("org-a", "/cart/")
        self.assertContains(resp, "Redeem with pass")
        self.assertNotContains(resp, "Apply</button>")

    def test_uncovered_hold_shows_not_covered_note_no_cta(self):
        self._redeeming_purchase(events=[self.event])
        self.post_as(
            "org-a",
            f"/performances/{self.other_performance.pk}/hold/",
            {"price_tier": self.other_tier.pk, "quantity": 1},
        )
        resp = self.get_as("org-a", "/cart/")
        self.assertContains(resp, "Not covered by your pass")
        self.assertNotContains(resp, "Redeem with pass")


class StaleSessionCleanupTests(TenantClientMixin, PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")

    def test_stale_pass_id_in_session_is_cleared_on_next_render(self):
        session = self.client.session
        session["redeeming_pass_id"] = 999999
        session.save()
        resp = self.get_as("org-a", "/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("redeeming_pass_id", self.client.session)
