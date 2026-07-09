"""Tests for the env-gated TEST CHECKOUT path: orders/views.py's
checkout_test view, reachable only when settings.ENABLE_TEST_CHECKOUT is
True. Exercises the exact same payments.services.fulfill_hold() core the
Stripe webhook uses (see payments/test_services.py + payments/test_views.py
for proof that refactor left the Stripe path's behavior unchanged), so these
tests focus on: the flag gate itself, buyer-info collection, GA + reserved
fulfillment via the HTTP view, availability/expiry rejection, and tenant/
session isolation.
"""

from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from events.models import GAAllocation
from orders.models import Hold, Order, Payment, Ticket
from orders.test_views import StorefrontFixtureMixin, TenantClientMixin
from venues.models import Seat


@override_settings(ENABLE_TEST_CHECKOUT=True)
class GATestCheckoutTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)

    def _create_hold(self, quantity=2):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": quantity},
        )
        return Hold.objects.get(performance=self.performance)

    def test_test_checkout_creates_order_tickets_payment_and_sends_email(self):
        hold = self._create_hold(quantity=2)

        resp = self.post_as(
            "org-a",
            "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "Test Buyer", "buyer_email": "buyer@example.com"},
        )

        order = Order.objects.get()
        self.assertRedirects(
            resp, f"/tickets/{order.token}/", fetch_redirect_response=False
        )
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.buyer_email, "buyer@example.com")
        self.assertEqual(order.buyer_name, "Test Buyer")
        self.assertEqual(order.total, Decimal("40.00"))  # 2 x $20
        self.assertIsNone(order.stripe_checkout_session_id)
        self.assertEqual(order.tickets.count(), 2)

        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.provider, "test")
        self.assertTrue(payment.provider_ref.startswith("test-"))
        self.assertEqual(payment.amount, Decimal("40.00"))

        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 2)
        self.assertFalse(Hold.objects.filter(pk=hold.pk).exists())

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])

    def test_reselling_same_hold_after_fulfillment_fails_cleanly(self):
        """The hold is deleted by the first fulfillment, so a resubmitted
        test-checkout POST for the same hold_id 404s (same lookup every
        other hold-consuming view in this module uses) instead of creating
        a second Order."""
        hold = self._create_hold(quantity=1)
        self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "First", "buyer_email": "first@example.com"},
        )
        self.assertEqual(Order.objects.count(), 1)

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "Second", "buyer_email": "second@example.com"},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Order.objects.count(), 1)

    def test_overselling_still_rejected(self):
        """Simulates GA availability changing out from under the hold
        between hold-creation and test-checkout (mirrors
        payments/test_services.py's equivalent Stripe-path test) -- the same
        re-check inside fulfill_hold() runs regardless of provider."""
        hold = self._create_hold(quantity=2)
        allocation = GAAllocation.objects.get(performance=self.performance)
        allocation.sold = allocation.capacity  # sold out from under the hold
        allocation.save(update_fields=["sold"])

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "Buyer", "buyer_email": "buyer@example.com"},
        )

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_expired_hold_rejected(self):
        hold = self._create_hold(quantity=1)
        hold.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        hold.save(update_fields=["expires_at"])

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "Buyer", "buyer_email": "buyer@example.com"},
        )

        self.assertEqual(resp.status_code, 404)  # expired hold no longer matches the lookup
        self.assertEqual(Order.objects.count(), 0)

    def test_missing_email_shows_error_and_creates_no_order(self):
        hold = self._create_hold(quantity=1)

        resp = self.post_as(
            "org-a", "/checkout/test/", {"hold_id": hold.pk, "buyer_name": "No Email"}
        )

        self.assertRedirects(resp, "/checkout/", fetch_redirect_response=False)
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())

    def test_cannot_fulfill_another_orgs_hold(self):
        org_b, venue_b = self.build_org("org-b")
        event_b, performance_b, tier_b = self.build_ga(org_b, venue_b, slug="show-b")
        hold_b = Hold.objects.create(
            organization=org_b,
            performance=performance_b,
            session_key="org-b-session",
            price_tier=tier_b,
            quantity=1,
        )

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold_b.pk, "buyer_name": "Cross Tenant", "buyer_email": "x@example.com"},
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold_b.pk).exists())

    def test_cannot_fulfill_another_sessions_hold(self):
        """Same org, but the hold belongs to a different browser
        session -- must not be reachable either."""
        other_client = self.client_class()
        other_client.post(
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
            HTTP_HOST="org-a.localhost",
        )
        other_hold = Hold.objects.get(performance=self.performance)

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": other_hold.pk, "buyer_name": "Not Mine", "buyer_email": "x@example.com"},
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Order.objects.count(), 0)


@override_settings(ENABLE_TEST_CHECKOUT=True)
class ReservedTestCheckoutTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.seat, self.tier = self.build_reserved(self.org, self.venue)

    def test_test_checkout_creates_one_ticket_per_seat(self):
        second_seat = Seat.objects.create(
            organization=self.org, section=self.seat.section, row_label="A", number="2"
        )
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"seat_id": [str(self.seat.pk), str(second_seat.pk)]},
        )
        hold = Hold.objects.get(performance=self.performance)

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "Seat Buyer", "buyer_email": "seatbuyer@example.com"},
        )

        order = Order.objects.get()
        self.assertRedirects(resp, f"/tickets/{order.token}/", fetch_redirect_response=False)
        self.assertEqual(order.tickets.count(), 2)
        seat_ids = set(order.tickets.values_list("seat_id", flat=True))
        self.assertEqual(seat_ids, {self.seat.id, second_seat.id})
        self.assertEqual(order.total, Decimal("100.00"))  # 2 x $50
        self.assertEqual(Payment.objects.get(order=order).provider, "test")
        self.assertFalse(Hold.objects.filter(pk=hold.pk).exists())

    def test_seat_already_ticketed_rejected(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"seat_id": [str(self.seat.pk)]},
        )
        hold = Hold.objects.get(performance=self.performance)

        # Simulate the seat becoming ticketed out from under the hold.
        other_order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="other@example.com",
            total=Decimal("50.00"),
            status=Order.Status.PAID,
        )
        Ticket.objects.create(
            organization=self.org, order=other_order, performance=self.performance, seat=self.seat
        )

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "Buyer", "buyer_email": "buyer@example.com"},
        )

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        self.assertEqual(Order.objects.filter(buyer_email="buyer@example.com").count(), 0)


class TestCheckoutDisabledByDefaultTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """settings.ENABLE_TEST_CHECKOUT defaults to False -- no override here,
    proving the route is inert with zero configuration."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)

    def test_route_404s_when_flag_is_off(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
        )
        hold = Hold.objects.get(performance=self.performance)

        resp = self.post_as(
            "org-a", "/checkout/test/",
            {"hold_id": hold.pk, "buyer_name": "Buyer", "buyer_email": "buyer@example.com"},
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())

    def test_checkout_page_does_not_show_test_pay_button(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
        )
        resp = self.get_as("org-a", "/checkout/")
        self.assertNotContains(resp, "Pay (TEST")

    def test_no_test_mode_banner(self):
        resp = self.get_as("org-a", "/")
        self.assertNotContains(resp, "TEST MODE")


@override_settings(ENABLE_TEST_CHECKOUT=True)
class TestCheckoutEnabledUITests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)

    def test_checkout_page_shows_test_pay_button(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
        )
        resp = self.get_as("org-a", "/checkout/")
        self.assertContains(resp, "Pay (TEST")

    def test_banner_shown_on_storefront(self):
        resp = self.get_as("org-a", "/")
        self.assertContains(resp, "TEST MODE")
