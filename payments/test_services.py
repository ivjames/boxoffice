"""Tests for payments/services.py: Checkout Session creation (line items,
minor-unit amounts, metadata, tenant URLs, per-org secret key) and webhook
fulfillment (idempotency, hold re-validation, GA/reserved ticket creation).
Every Stripe SDK call is monkeypatched -- these tests never hit the network.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils import timezone

from orders import services as order_services
from orders.models import Hold, Order, Payment, Ticket
from orders.tests import OrdersFixtureMixin
from payments import services
from venues.models import Seat


def host_for(subdomain):
    return f"{subdomain}.localhost"


class FakeStripeSession:
    """Minimal stand-in for what stripe.checkout.Session.create() returns."""

    def __init__(self, url="https://checkout.stripe.com/pay/cs_test_123"):
        self.url = url
        self.id = "cs_test_123"


class CreateCheckoutSessionTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_ga_performance()
        self.org.stripe_secret_key = "sk_test_org_a_secret"
        self.org.save(update_fields=["stripe_secret_key"])
        self.hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))

    @patch("payments.services.stripe.checkout.Session.create")
    def test_ga_session_uses_org_secret_key_and_metadata(self, mock_create):
        mock_create.return_value = FakeStripeSession()

        url = services.create_checkout_session(self.hold, self.request)

        self.assertEqual(url, "https://checkout.stripe.com/pay/cs_test_123")
        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs["api_key"], "sk_test_org_a_secret")
        self.assertEqual(kwargs["mode"], "payment")
        self.assertEqual(
            kwargs["metadata"], {"hold_id": str(self.hold.pk), "organization_id": str(self.org.pk)}
        )

    def test_ga_session_success_and_cancel_urls_are_on_tenant_subdomain(self):
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            services.create_checkout_session(self.hold, self.request)
            _, kwargs = mock_create.call_args

        host = host_for(self.org.subdomain)
        self.assertTrue(kwargs["success_url"].startswith(f"http://{host}/checkout/success/"))
        self.assertIn("session_id={CHECKOUT_SESSION_ID}", kwargs["success_url"])
        self.assertEqual(kwargs["cancel_url"], f"http://{host}/checkout/cancel/")

    def test_ga_line_item_quantity_and_integer_minor_unit_amount(self):
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            services.create_checkout_session(self.hold, self.request)
            _, kwargs = mock_create.call_args

        line_items = kwargs["line_items"]
        self.assertEqual(len(line_items), 1)
        item = line_items[0]
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["price_data"]["unit_amount"], 3500)  # $35.00 tier -> integer cents
        self.assertIsInstance(item["price_data"]["unit_amount"], int)
        self.assertEqual(item["price_data"]["currency"], "usd")
        self.assertIn(self.performance.event.title, item["price_data"]["product_data"]["name"])

    def test_expired_hold_raises_checkout_error_without_calling_stripe(self):
        self.hold.expires_at = timezone.now() - timedelta(minutes=1)
        self.hold.save(update_fields=["expires_at"])

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            with self.assertRaises(services.CheckoutError):
                services.create_checkout_session(self.hold, self.request)
            mock_create.assert_not_called()


class CreateCheckoutSessionReservedHoldTests(OrdersFixtureMixin, TestCase):
    """Separate class (own setUp/org) from CreateCheckoutSessionTests: that
    class's setUp already builds a GA fixture under subdomain "roxy", and
    OrdersFixtureMixin.build_reserved_performance() would try to create a
    second Organization with the same subdomain in the same test."""

    def setUp(self):
        self.build_reserved_performance()
        self.org.stripe_secret_key = "sk_test_org_a_secret"
        self.org.save(update_fields=["stripe_secret_key"])
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))

    def test_reserved_hold_has_one_line_item_per_seat(self):
        second_seat = Seat.objects.create(
            organization=self.org, section=self.section, row_label="A", number="2"
        )
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-c",
            user=None,
            seat_ids=[self.seat.id, second_seat.id],
        )

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            services.create_checkout_session(hold, self.request)
            _, kwargs = mock_create.call_args

        line_items = kwargs["line_items"]
        self.assertEqual(len(line_items), 2)
        for item in line_items:
            self.assertEqual(item["quantity"], 1)
            self.assertEqual(item["price_data"]["unit_amount"], 6500)  # $65.00 tier


class FulfillCheckoutSessionTests(OrdersFixtureMixin, TestCase):
    """Note: OrdersFixtureMixin's build_ga_performance()/build_reserved_performance()
    each create a fresh Organization (subdomain "roxy") -- a test needing
    the reserved fixture must NOT also call the GA one (or vice versa), or
    the second call 409s on the unique subdomain. Tests that need the GA
    hold call _build_ga_hold(); the two reserved-seat tests build their own
    fixtures directly."""

    def _build_ga_hold(self, quantity=2):
        self.build_ga_performance()
        return order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=quantity,
        )

    def _session(self, hold, session_id="cs_test_abc", email="buyer@example.com", name="Buyer Person"):
        return {
            "id": session_id,
            "payment_intent": "pi_test_abc",
            "metadata": {"hold_id": str(hold.pk), "organization_id": str(hold.organization_id)},
            "customer_details": {"email": email, "name": name},
        }

    def test_creates_order_items_tickets_and_payment(self):
        self.hold = self._build_ga_hold()
        session = self._session(self.hold)

        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.buyer_email, "buyer@example.com")
        self.assertEqual(order.buyer_name, "Buyer Person")
        self.assertEqual(order.total, Decimal("70.00"))  # 2 x $35
        self.assertEqual(order.stripe_checkout_session_id, "cs_test_abc")
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.tickets.count(), 2)
        self.assertTrue(all(t.seat_id is None for t in order.tickets.all()))
        self.assertEqual(Payment.objects.filter(order=order, provider="stripe").count(), 1)
        self.assertEqual(Payment.objects.get(order=order).provider_ref, "pi_test_abc")

        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 2)
        self.assertFalse(Hold.objects.filter(pk=self.hold.pk).exists())

    def test_replaying_same_session_is_idempotent(self):
        self.hold = self._build_ga_hold()
        session = self._session(self.hold)

        order1, created1 = services.fulfill_checkout_session(self.org, session)
        order2, created2 = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(order1.pk, order2.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Ticket.objects.count(), 2)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 2)  # not double-incremented

    def test_gone_hold_raises_and_creates_no_order(self):
        self.hold = self._build_ga_hold()
        session = self._session(self.hold)
        self.hold.delete()

        with self.assertRaises(services.HoldGoneError):
            services.fulfill_checkout_session(self.org, session)

        self.assertEqual(Order.objects.count(), 0)

    def test_expired_hold_raises_and_creates_no_order(self):
        self.hold = self._build_ga_hold()
        session = self._session(self.hold)
        self.hold.expires_at = timezone.now() - timedelta(minutes=1)
        self.hold.save(update_fields=["expires_at"])

        with self.assertRaises(services.HoldGoneError):
            services.fulfill_checkout_session(self.org, session)

        self.assertEqual(Order.objects.count(), 0)
        # The (still-expired) hold is untouched -- fulfillment never got far
        # enough to delete it.
        self.assertTrue(Hold.objects.filter(pk=self.hold.pk).exists())

    def test_mismatched_metadata_organization_raises(self):
        from venues.tests import make_org

        self.hold = self._build_ga_hold()
        other_org = make_org("org-mismatch")
        session = self._session(self.hold)
        session["metadata"]["organization_id"] = str(other_org.pk)

        with self.assertRaises(services.TenantMismatchError):
            services.fulfill_checkout_session(self.org, session)
        self.assertEqual(Order.objects.count(), 0)

    def test_reserved_hold_creates_one_ticket_per_seat(self):
        self.build_reserved_performance()
        second_seat = Seat.objects.create(
            organization=self.org, section=self.section, row_label="A", number="2"
        )
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-b",
            user=None,
            seat_ids=[self.seat.id, second_seat.id],
        )
        session = self._session(hold, session_id="cs_test_reserved")

        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.tickets.count(), 2)
        seat_ids = set(order.tickets.values_list("seat_id", flat=True))
        self.assertEqual(seat_ids, {self.seat.id, second_seat.id})
        self.assertEqual(order.total, Decimal("130.00"))  # 2 x $65

    def test_seat_already_ticketed_raises_availability_changed(self):
        self.build_reserved_performance()
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-b",
            user=None,
            seat_ids=[self.seat.id],
        )
        # Simulate the seat somehow already being ticketed by the time this
        # fulfillment runs (shouldn't normally happen -- defensive recheck).
        other_order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="other@example.com",
            total=Decimal("65.00"),
            status=Order.Status.PAID,
        )
        Ticket.objects.create(
            organization=self.org, order=other_order, performance=self.performance, seat=self.seat
        )
        session = self._session(hold, session_id="cs_test_conflict")

        with self.assertRaises(services.AvailabilityChangedError):
            services.fulfill_checkout_session(self.org, session)
        self.assertEqual(Order.objects.filter(stripe_checkout_session_id="cs_test_conflict").count(), 0)
