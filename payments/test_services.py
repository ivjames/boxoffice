"""Tests for payments/services.py: Checkout Session creation (line items,
minor-unit amounts, metadata, tenant URLs, Connect direct charge + platform
fee) and webhook fulfillment (idempotency, hold re-validation, GA/reserved
ticket creation). Every Stripe SDK call is monkeypatched -- these tests never
hit the network.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from events.models import PricingZone, ZoneTemplate
from orders import services as order_services
from orders.models import Hold, Order, Payment, Ticket
from orders.tests import OrdersFixtureMixin
from payments import services
from promotions.models import PromoCode
from venues.models import Seat

# The platform key every Connect call authenticates with, and a stand-in
# connected-account id for the test org. create_checkout_session passes the
# platform key as `api_key` and the org's account as `stripe_account` (a direct
# charge). Overridden into settings where the key value itself is asserted.
PLATFORM_KEY = "sk_test_platform"
TEST_ACCT = "acct_test_org_a"


def host_for(subdomain):
    return f"{subdomain}.localhost"


def enable_connect(org, account_id=TEST_ACCT):
    """Put `org` in the "can take real payments" state: a connected account
    with charges enabled. The equivalent of the theater having finished Stripe
    Connect onboarding."""
    org.stripe_account_id = account_id
    org.stripe_charges_enabled = True
    org.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])


class FakeStripeSession:
    """Minimal stand-in for what stripe.checkout.Session.create() returns."""

    def __init__(self, url="https://checkout.stripe.com/pay/cs_test_123"):
        self.url = url
        self.id = "cs_test_123"


class FakeCoupon:
    """Minimal stand-in for what stripe.Coupon.create() returns -- the promo
    discount path attaches its `.id` to the Session's `discounts` param."""

    def __init__(self, coupon_id="coupon_test_123"):
        self.id = coupon_id


@override_settings(STRIPE_SECRET_KEY=PLATFORM_KEY)
class CreateCheckoutSessionTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_ga_performance()
        enable_connect(self.org)
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
    def test_ga_session_is_a_direct_charge_with_platform_key_and_metadata(self, mock_create):
        mock_create.return_value = FakeStripeSession()

        url = services.create_checkout_session(self.hold, self.request)

        self.assertEqual(url, "https://checkout.stripe.com/pay/cs_test_123")
        _, kwargs = mock_create.call_args
        # Platform key authenticates; stripe_account selects the theater's
        # connected account (the direct charge).
        self.assertEqual(kwargs["api_key"], PLATFORM_KEY)
        self.assertEqual(kwargs["stripe_account"], TEST_ACCT)
        self.assertEqual(kwargs["mode"], "payment")
        self.assertEqual(
            kwargs["metadata"], {"hold_id": str(self.hold.pk), "organization_id": str(self.org.pk)}
        )

    @patch("payments.services.stripe.checkout.Session.create")
    def test_no_application_fee_when_rate_is_zero(self, mock_create):
        """The launch default (PLATFORM_FEE_PERCENT/FIXED_CENTS both 0) sends
        NO application fee -- Stripe rejects an explicit fee of 0, and "no cut
        yet" is intended. payment_intent_data is omitted entirely."""
        mock_create.return_value = FakeStripeSession()

        services.create_checkout_session(self.hold, self.request)

        _, kwargs = mock_create.call_args
        self.assertNotIn("payment_intent_data", kwargs)

    @override_settings(PLATFORM_FEE_PERCENT=10, PLATFORM_FEE_FIXED_CENTS=0)
    @patch("payments.services.stripe.checkout.Session.create")
    def test_application_fee_from_global_percent(self, mock_create):
        """10% of the 2 x $35 = $70 order -> $7.00 -> 700 minor units, set as
        the application fee on the direct charge's PaymentIntent."""
        mock_create.return_value = FakeStripeSession()

        services.create_checkout_session(self.hold, self.request)

        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs["payment_intent_data"]["application_fee_amount"], 700)

    @override_settings(PLATFORM_FEE_PERCENT=10)
    @patch("payments.services.stripe.checkout.Session.create")
    def test_per_org_fee_override_wins_over_global(self, mock_create):
        """A per-theater platform_fee_percent overrides the global default:
        5% of $70 -> 350 minor units, not the global 10%'s 700."""
        self.org.platform_fee_percent = Decimal("5")
        self.org.save(update_fields=["platform_fee_percent"])
        mock_create.return_value = FakeStripeSession()

        services.create_checkout_session(self.hold, self.request)

        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs["payment_intent_data"]["application_fee_amount"], 350)

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

    def test_charges_not_enabled_returns_stub_url_without_calling_stripe(self):
        """A tenant that hasn't finished Connect onboarding (charges not
        enabled) can't take a real payment. create_checkout_session returns the
        internal simulated-checkout stub URL and never touches Stripe, so the
        browse -> buy -> ticket demo flow still works pre-launch."""
        self.org.stripe_charges_enabled = False
        self.org.save(update_fields=["stripe_charges_enabled"])

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            url = services.create_checkout_session(self.hold, self.request)
            mock_create.assert_not_called()

        self.assertIn("/checkout/stub/", url)
        self.assertIn(f"hold_id={self.hold.pk}", url)


class CreateCheckoutSessionReservedHoldTests(OrdersFixtureMixin, TestCase):
    """Separate class (own setUp/org) from CreateCheckoutSessionTests: that
    class's setUp already builds a GA fixture under subdomain "roxy", and
    OrdersFixtureMixin.build_reserved_performance() would try to create a
    second Organization with the same subdomain in the same test."""

    def setUp(self):
        self.build_reserved_performance()
        enable_connect(self.org)
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))

    def test_reserved_hold_honors_a_per_performance_price_override(self):
        """events/pricing.py resolve_seat_tier's override must flow all the
        way through to the Stripe line item: set_reserved_hold resolves it
        onto HoldSeat.price_tier, and _line_items_for_hold reads that field
        directly -- no separate price lookup at checkout time."""
        from events.models import PriceTier

        PriceTier.objects.create(
            organization=self.org,
            performance=self.performance,
            section=self.section,
            name="Orchestra (evening premium)",
            amount=Decimal("85.00"),
        )
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-override",
            user=None,
            seat_ids=[self.seat.id],
        )

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            services.create_checkout_session(hold, self.request)
            _, kwargs = mock_create.call_args

        line_items = kwargs["line_items"]
        self.assertEqual(len(line_items), 1)
        self.assertEqual(line_items[0]["price_data"]["unit_amount"], 8500)  # $85.00 override

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

    def test_fulfillment_charges_the_override_price_not_the_section_default(self):
        from events.models import PriceTier

        self.build_reserved_performance()  # section default: $65.00
        PriceTier.objects.create(
            organization=self.org,
            performance=self.performance,
            section=self.section,
            name="Orchestra (evening premium)",
            amount=Decimal("85.00"),
        )
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-override",
            user=None,
            seat_ids=[self.seat.id],
        )
        session = self._session(hold, session_id="cs_test_override")

        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("85.00"))
        item = order.items.get()
        self.assertEqual(item.unit_amount, Decimal("85.00"))
        self.assertEqual(item.price_tier.amount, Decimal("85.00"))

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


class MinorUnitsCurrencyTests(TestCase):
    """_to_minor_units and application_fee_amount must pick the minor-unit
    exponent by currency (BO-6), so a zero-decimal currency (JPY) isn't
    charged/fee'd 100x too much."""

    def test_two_decimal_currency_uses_cents(self):
        self.assertEqual(services._to_minor_units(Decimal("35.00"), "USD"), 3500)

    def test_zero_decimal_currency_uses_whole_units(self):
        self.assertEqual(services._to_minor_units(Decimal("500.00"), "JPY"), 500)

    def test_three_decimal_currency_rounds_to_multiple_of_ten(self):
        # 12.345 KWD -> 12345 thousandths -> 12340 (Stripe wants a multiple of 10).
        self.assertEqual(services._to_minor_units(Decimal("12.345"), "KWD"), 12340)

    @override_settings(PLATFORM_FEE_PERCENT=10, PLATFORM_FEE_FIXED_CENTS=0)
    def test_fee_two_decimal_currency_unchanged(self):
        org = SimpleNamespace(platform_fee_percent=None, currency="USD")
        # 10% of $70 -> 700 minor units (unchanged from the pre-BO-6 behavior).
        self.assertEqual(services.application_fee_amount(org, Decimal("70.00"), currency="USD"), 700)

    @override_settings(PLATFORM_FEE_PERCENT=10, PLATFORM_FEE_FIXED_CENTS=0)
    def test_fee_zero_decimal_currency_not_scaled_100x(self):
        org = SimpleNamespace(platform_fee_percent=None, currency="JPY")
        # 10% of ¥4000 -> ¥400 -> 400 minor units, NOT 40000.
        self.assertEqual(services.application_fee_amount(org, Decimal("4000"), currency="JPY"), 400)


class FulfillmentIntegrityErrorMappingTests(OrdersFixtureMixin, TestCase):
    """A non-dup-session IntegrityError during fulfillment (e.g. the live-
    ticket-per-seat unique constraint firing on a concurrent ticketing) must
    surface as AvailabilityChangedError -- a FulfillmentError the webhook acks
    with 200 -- not a bare IntegrityError that 500s into a 3-day Stripe retry
    loop (BO-6)."""

    def _session(self, hold, session_id="cs_test_ie"):
        return {
            "id": session_id,
            "payment_intent": "pi_test_ie",
            "metadata": {"hold_id": str(hold.pk), "organization_id": str(hold.organization_id)},
            "customer_details": {"email": "buyer@example.com", "name": "Buyer"},
        }

    def test_integrity_error_without_a_winner_maps_to_availability_changed(self):
        self.build_ga_performance()
        hold = order_services.set_ga_hold(
            organization=self.org, performance=self.performance, session_key="sess-ie",
            user=None, price_tier=self.price_tier, quantity=1,
        )
        session = self._session(hold)

        with patch(
            "payments.services.fulfill_hold", side_effect=IntegrityError("seat conflict")
        ):
            with self.assertRaises(services.AvailabilityChangedError):
                services.fulfill_checkout_session(self.org, session)
        self.assertEqual(Order.objects.filter(stripe_checkout_session_id="cs_test_ie").count(), 0)


class StripeSessionIdempotencyRaceTests(OrdersFixtureMixin, TestCase):
    """fulfill_checkout_session()'s idempotency (test_replaying_same_session_is_idempotent
    above) is correct for a SEQUENTIAL replay -- the second call's initial
    "does an Order already exist" check finds the first call's already-
    committed Order and returns early. That check isn't itself locked,
    though: two truly concurrent deliveries (real duplicate Stripe webhook
    delivery hitting two gunicorn workers at once) can both read "no Order
    yet" before either commits. Under Postgres's default READ COMMITTED
    isolation this is a real race; SQLite's harden_sqlite() IMMEDIATE-mode
    whole-database lock is the only thing preventing it on today's default
    deployment (see orders/test_concurrency_multiprocess.py's module
    docstring for why a real multi-connection race can't be reproduced
    against pytest's in-memory SQLite test database either way). These
    tests instead prove the DB-level backstop directly: first, that the
    constraint itself rejects a duplicate (organization,
    stripe_checkout_session_id) pair; second, that fulfill_checkout_session
    survives hitting it mid-race (by forcing its own pre-check to report
    "not found", which is exactly what it would legitimately see in the
    race window) without creating a second Order/Ticket set or crashing the
    webhook.
    """

    def test_db_rejects_a_second_order_for_the_same_org_and_session_id(self):
        self.build_ga_performance()
        Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="first@example.com",
            total=Decimal("10.00"),
            status=Order.Status.PAID,
            stripe_checkout_session_id="cs_dup",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(
                    organization=self.org,
                    performance=self.performance,
                    buyer_email="second@example.com",
                    total=Decimal("10.00"),
                    status=Order.Status.PAID,
                    stripe_checkout_session_id="cs_dup",
                )

    def test_multiple_orders_without_a_session_id_are_still_allowed(self):
        """The constraint's exclusion condition must not block ordinary
        manually-created/pending Orders that have no Stripe session id."""
        self.build_ga_performance()
        Order.objects.create(
            organization=self.org, performance=self.performance,
            buyer_email="a@example.com", total=Decimal("10.00"),
        )
        Order.objects.create(
            organization=self.org, performance=self.performance,
            buyer_email="b@example.com", total=Decimal("10.00"),
        )
        from django.db.models import Q

        no_session_id = Q(stripe_checkout_session_id__isnull=True) | Q(stripe_checkout_session_id="")
        self.assertEqual(Order.objects.filter(no_session_id).count(), 2)

    def _build_ga_hold(self, quantity=2):
        self.build_ga_performance()
        return order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-race",
            user=None,
            price_tier=self.price_tier,
            quantity=quantity,
        )

    def _session(self, hold, session_id="cs_test_race", email="buyer@example.com", name="Buyer Person"):
        return {
            "id": session_id,
            "payment_intent": "pi_test_race",
            "metadata": {"hold_id": str(hold.pk), "organization_id": str(hold.organization_id)},
            "customer_details": {"email": email, "name": name},
        }

    def test_concurrent_duplicate_delivery_falls_back_to_the_committed_winner(self):
        """Simulates the real race: two "concurrent" webhook deliveries for
        the same session id where BOTH pass fulfill_checkout_session's
        initial existence check before either has committed. Patches that
        pre-check to report "not found" for this call (exactly what it
        would legitimately see mid-race), after a competing Order for the
        same session id has already been committed by "the other worker" --
        proving the DB constraint + graceful IntegrityError handling is
        what actually prevents a second Order/Ticket set and a double
        GAAllocation.sold bump, not just the ordering of test assertions.
        """
        self.hold = self._build_ga_hold()
        session = self._session(self.hold)

        winner = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="winner@example.com",
            buyer_name="Winner",
            total=Decimal("70.00"),
            status=Order.Status.PAID,
            stripe_checkout_session_id=session["id"],
        )

        # Only the FIRST Order.objects.filter() call (fulfill_checkout_session's
        # own pre-check) is faked out to miss -- exactly what it would
        # legitimately see mid-race. The except-block's post-IntegrityError
        # lookup must hit the real table (that's the fallback under test).
        real_filter = Order.objects.filter
        calls = {"n": 0}

        def fake_filter(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                from unittest.mock import MagicMock

                mock_qs = MagicMock()
                mock_qs.first.return_value = None
                return mock_qs
            return real_filter(*args, **kwargs)

        with patch("payments.services.Order.objects.filter", side_effect=fake_filter):
            order, created = services.fulfill_checkout_session(self.org, session)

        self.assertFalse(created)
        self.assertEqual(order.pk, winner.pk)
        self.assertEqual(Order.objects.filter(stripe_checkout_session_id=session["id"]).count(), 1)
        # The losing branch's fulfillment never ran: no extra tickets, GA
        # allocation untouched, and the Hold it would have consumed
        # survives (its transaction rolled back to the savepoint, not
        # forward into _fulfill_ga/hold.delete()).
        self.assertEqual(Ticket.objects.count(), 0)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 0)
        self.assertTrue(Hold.objects.filter(pk=self.hold.pk).exists())


class ZonePricingCheckoutAndFulfillmentTests(OrdersFixtureMixin, TestCase):
    """Phase C (docs/SEATING.md "C") end-to-end money-path proof: a seat in
    a PricingZone charges the zone price all the way from hold creation
    through the Stripe line item to the fulfilled OrderItem/Ticket, and a
    zone/template edit -- or the zone being deleted outright -- AFTER the
    hold was created never changes what actually gets charged, because
    every read past hold-creation goes through HoldSeat.unit_amount (the
    snapshot), never a live zone/tier lookup."""

    def setUp(self):
        self.build_reserved_performance()  # section default tier: $65.00
        enable_connect(self.org)
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))
        self.template = ZoneTemplate.objects.create(
            organization=self.org, name="Premium", color="#c1121f"
        )
        self.zone = PricingZone.objects.create(
            organization=self.org,
            performance=self.performance,
            template=self.template,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        self.zone.seats.add(self.seat, through_defaults={"organization": self.org})

    def _session(self, hold, session_id="cs_test_zone", email="buyer@example.com", name="Buyer Person"):
        return {
            "id": session_id,
            "payment_intent": "pi_test_zone",
            "metadata": {"hold_id": str(hold.pk), "organization_id": str(hold.organization_id)},
            "customer_details": {"email": email, "name": name},
        }

    def test_checkout_line_item_charges_the_zone_price(self):
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-zone",
            user=None,
            seat_ids=[self.seat.id],
        )
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            services.create_checkout_session(hold, self.request)
            _, kwargs = mock_create.call_args

        line_items = kwargs["line_items"]
        self.assertEqual(len(line_items), 1)
        self.assertEqual(line_items[0]["price_data"]["unit_amount"], 9500)  # $95.00 zone price

    def test_fulfillment_creates_order_item_with_zone_provenance_and_snapshot_amount(self):
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-zone",
            user=None,
            seat_ids=[self.seat.id],
        )
        session = self._session(hold)

        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("95.00"))
        item = order.items.get()
        self.assertEqual(item.unit_amount, Decimal("95.00"))
        self.assertEqual(item.pricing_zone_id, self.zone.pk)
        self.assertIsNone(item.price_tier_id)
        self.assertEqual(order.tickets.get().seat_id, self.seat.id)

    def test_editing_the_zone_after_hold_creation_does_not_change_the_charge(self):
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-zone",
            user=None,
            seat_ids=[self.seat.id],
        )
        # The price changes AFTER the hold snapshotted it, before payment.
        self.zone.amount = Decimal("500.00")
        self.zone.save(update_fields=["amount"])

        session = self._session(hold)
        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("95.00"))
        self.assertEqual(order.items.get().unit_amount, Decimal("95.00"))

    def test_deleting_the_zone_after_hold_creation_does_not_change_the_charge(self):
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-zone",
            user=None,
            seat_ids=[self.seat.id],
        )
        self.zone.delete()

        session = self._session(hold)
        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("95.00"))
        item = order.items.get()
        self.assertEqual(item.unit_amount, Decimal("95.00"))
        self.assertIsNone(item.pricing_zone_id)
        self.assertIsNone(item.price_tier_id)

    def test_unzoned_seat_on_same_performance_still_charges_section_default(self):
        second_seat = Seat.objects.create(
            organization=self.org, section=self.section, row_label="A", number="2"
        )
        hold = order_services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-mixed",
            user=None,
            seat_ids=[self.seat.id, second_seat.id],
        )
        session = self._session(hold, session_id="cs_test_mixed")

        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("160.00"))  # $95 zone + $65 section default
        zoned_item = order.items.get(seat=self.seat)
        unzoned_item = order.items.get(seat=second_seat)
        self.assertEqual(zoned_item.unit_amount, Decimal("95.00"))
        self.assertEqual(unzoned_item.unit_amount, Decimal("65.00"))
        self.assertIsNotNone(unzoned_item.price_tier_id)
        self.assertIsNone(unzoned_item.pricing_zone_id)


# --- Promo-code money path (Stripe coupon + net fee + fulfillment) ------------
#
# create_checkout_session mints a per-session stripe.Coupon (amount_off = the
# hold's snapshotted discount in minor units, on the CONNECTED account) and
# passes it as `discounts`, while line items stay GROSS; the application fee is
# charged on the NET (post-discount) total; fulfill_hold records Order.total /
# Payment.amount as the net and bumps redemption_count exactly once.


def _add_promo(org, *, code="TENOFF", kind=PromoCode.Kind.FIXED, value="10", currency=""):
    return PromoCode.objects.create(
        organization=org, code=code, kind=kind, value=Decimal(value), currency=currency
    )


@override_settings(STRIPE_SECRET_KEY=PLATFORM_KEY)
class DiscountedCheckoutSessionTests(OrdersFixtureMixin, TestCase):
    """GA fixture: 2 x $35 tier = $70 gross. Coupon + net-fee behavior."""

    def setUp(self):
        self.build_ga_performance()
        enable_connect(self.org)
        self.hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))

    def _apply(self, **kwargs):
        _add_promo(self.org, **kwargs)
        return order_services.apply_promo_code(
            organization=self.org,
            session_key="sess-a",
            hold_id=self.hold.pk,
            code=kwargs.get("code", "TENOFF"),
        )

    @patch("payments.services.stripe.Coupon.create")
    @patch("payments.services.stripe.checkout.Session.create")
    def test_discount_mints_a_connected_account_coupon_and_attaches_it(self, mock_session, mock_coupon):
        mock_session.return_value = FakeStripeSession()
        mock_coupon.return_value = FakeCoupon()
        hold = self._apply(kind=PromoCode.Kind.FIXED, value="10")  # $10 off $70

        services.create_checkout_session(hold, self.request)

        # Coupon minted on the CONNECTED account with the platform key, in minor
        # units of the $10.00 discount, once-only.
        _, ckwargs = mock_coupon.call_args
        self.assertEqual(ckwargs["amount_off"], 1000)
        self.assertEqual(ckwargs["currency"], "usd")
        self.assertEqual(ckwargs["duration"], "once")
        self.assertEqual(ckwargs["api_key"], PLATFORM_KEY)
        self.assertEqual(ckwargs["stripe_account"], TEST_ACCT)

        # And attached to the Session, whose line items are still GROSS.
        _, skwargs = mock_session.call_args
        self.assertEqual(skwargs["discounts"], [{"coupon": "coupon_test_123"}])
        line_items = skwargs["line_items"]
        self.assertEqual(len(line_items), 1)
        self.assertEqual(line_items[0]["quantity"], 2)
        self.assertEqual(line_items[0]["price_data"]["unit_amount"], 3500)  # gross, undiscounted

    @patch("payments.services.stripe.Coupon.create")
    @patch("payments.services.stripe.checkout.Session.create")
    def test_percent_discount_amount_off(self, mock_session, mock_coupon):
        mock_session.return_value = FakeStripeSession()
        mock_coupon.return_value = FakeCoupon()
        hold = self._apply(code="SAVE10", kind=PromoCode.Kind.PERCENT, value="10")  # 10% of $70 = $7

        services.create_checkout_session(hold, self.request)

        _, ckwargs = mock_coupon.call_args
        self.assertEqual(ckwargs["amount_off"], 700)

    @patch("payments.services.stripe.Coupon.create")
    @patch("payments.services.stripe.checkout.Session.create")
    def test_no_promo_never_calls_coupon_and_omits_discounts(self, mock_session, mock_coupon):
        mock_session.return_value = FakeStripeSession()

        services.create_checkout_session(self.hold, self.request)

        mock_coupon.assert_not_called()
        _, skwargs = mock_session.call_args
        self.assertNotIn("discounts", skwargs)

    @override_settings(PLATFORM_FEE_PERCENT=10, PLATFORM_FEE_FIXED_CENTS=0)
    @patch("payments.services.stripe.Coupon.create")
    @patch("payments.services.stripe.checkout.Session.create")
    def test_application_fee_is_charged_on_the_net_total(self, mock_session, mock_coupon):
        """10% fee on the $70 gross with a $10 discount is charged on the $60
        NET -> 600 minor units, NOT 700. Otherwise a discounted order would
        over-charge the fee against money that never changed hands."""
        mock_session.return_value = FakeStripeSession()
        mock_coupon.return_value = FakeCoupon()
        hold = self._apply(kind=PromoCode.Kind.FIXED, value="10")

        services.create_checkout_session(hold, self.request)

        _, skwargs = mock_session.call_args
        self.assertEqual(skwargs["payment_intent_data"]["application_fee_amount"], 600)


@override_settings(STRIPE_SECRET_KEY=PLATFORM_KEY)
class DiscountedCheckoutCurrencyTests(OrdersFixtureMixin, TestCase):
    """The coupon amount_off must be denominated in the charge currency's minor
    units -- whole units for a zero-decimal currency (JPY), thousandths (x1000,
    a multiple of 10) for a three-decimal currency (KWD) -- not blindly x100."""

    def _build_ga_hold_in(self, currency, tier_amount, quantity=2):
        self.build_ga_performance()
        enable_connect(self.org)
        self.price_tier.currency = currency
        self.price_tier.amount = Decimal(tier_amount)
        self.price_tier.save(update_fields=["currency", "amount"])
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))
        return order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=quantity,
        )

    def _apply(self, hold, *, value, currency):
        _add_promo(self.org, kind=PromoCode.Kind.FIXED, value=value, currency=currency)
        return order_services.apply_promo_code(
            organization=self.org, session_key="sess-a", hold_id=hold.pk, code="TENOFF"
        )

    @patch("payments.services.stripe.Coupon.create")
    @patch("payments.services.stripe.checkout.Session.create")
    def test_zero_decimal_currency_amount_off_is_whole_units(self, mock_session, mock_coupon):
        mock_session.return_value = FakeStripeSession()
        mock_coupon.return_value = FakeCoupon()
        # ¥5000 tier x2 = ¥10000 gross; ¥500 off.
        hold = self._build_ga_hold_in("JPY", "5000.00")
        hold = self._apply(hold, value="500", currency="JPY")

        services.create_checkout_session(hold, self.request)

        _, ckwargs = mock_coupon.call_args
        self.assertEqual(ckwargs["amount_off"], 500)  # whole yen, NOT 50000
        self.assertEqual(ckwargs["currency"], "jpy")

    @patch("payments.services.stripe.Coupon.create")
    @patch("payments.services.stripe.checkout.Session.create")
    def test_three_decimal_currency_amount_off_is_thousandths_multiple_of_ten(
        self, mock_session, mock_coupon
    ):
        mock_session.return_value = FakeStripeSession()
        mock_coupon.return_value = FakeCoupon()
        # 50.000 KWD tier x2 = 100 KWD gross; 5.00 KWD off.
        hold = self._build_ga_hold_in("KWD", "50.00")
        hold = self._apply(hold, value="5", currency="KWD")

        services.create_checkout_session(hold, self.request)

        _, ckwargs = mock_coupon.call_args
        # 5.00 KWD -> 5000 thousandths (x1000, NOT x100's 500); a multiple of 10.
        self.assertEqual(ckwargs["amount_off"], 5000)
        self.assertEqual(ckwargs["amount_off"] % 10, 0)
        self.assertEqual(ckwargs["currency"], "kwd")


class DiscountedFulfillmentTests(OrdersFixtureMixin, TestCase):
    """fulfill_hold (via the Stripe webhook AND the shared core the test path
    uses) records the NET total, snapshots the promo onto the Order, and bumps
    redemption_count exactly once -- and NEVER rejects a paid order for promo
    state (soft cap)."""

    def _build_discounted_ga_hold(self, *, value="10", kind=PromoCode.Kind.FIXED, quantity=2, code="TENOFF"):
        self.build_ga_performance()  # 2 x $35 = $70 gross
        hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=quantity,
        )
        self.promo = _add_promo(self.org, code=code, kind=kind, value=value)
        return order_services.apply_promo_code(
            organization=self.org, session_key="sess-a", hold_id=hold.pk, code=code
        )

    def _session(self, hold, session_id="cs_test_promo"):
        return {
            "id": session_id,
            "payment_intent": "pi_test_promo",
            "metadata": {"hold_id": str(hold.pk), "organization_id": str(hold.organization_id)},
            "customer_details": {"email": "buyer@example.com", "name": "Buyer Person"},
        }

    def test_webhook_fulfillment_records_net_total_and_snapshots_promo(self):
        hold = self._build_discounted_ga_hold(value="10")  # $10 off $70 -> $60 net

        order, created = services.fulfill_checkout_session(self.org, self._session(hold))

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("60.00"))  # NET, not the $70 gross
        self.assertEqual(order.discount_amount, Decimal("10.00"))
        self.assertEqual(order.promo_code_text, "TENOFF")
        self.assertEqual(Payment.objects.get(order=order).amount, Decimal("60.00"))
        self.promo.refresh_from_db()
        self.assertEqual(self.promo.redemption_count, 1)  # bumped exactly once

    def test_redemption_counted_only_once(self):
        hold = self._build_discounted_ga_hold(value="10")
        services.fulfill_checkout_session(self.org, self._session(hold))
        # Replaying the same session is idempotent -> must NOT double-count.
        services.fulfill_checkout_session(self.org, self._session(hold))
        self.promo.refresh_from_db()
        self.assertEqual(self.promo.redemption_count, 1)

    def test_maxed_out_between_apply_and_fulfill_still_fulfills_and_increments(self):
        """A paid order is NEVER rejected for promo state: the code may have hit
        its cap in the window between apply-to-cart and payment, but the buyer
        already paid the discounted amount -- fulfillment does not re-validate,
        and still records the redemption."""
        hold = self._build_discounted_ga_hold(value="10")
        # The code maxes out after it was applied but before this payment lands.
        self.promo.max_redemptions = 5
        self.promo.redemption_count = 5
        self.promo.save(update_fields=["max_redemptions", "redemption_count"])

        order, created = services.fulfill_checkout_session(self.org, self._session(hold))

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("60.00"))
        self.promo.refresh_from_db()
        self.assertEqual(self.promo.redemption_count, 6)  # still incremented, over cap

    def test_test_provider_path_records_net_and_increments(self):
        """The env-gated test-checkout path shares fulfill_hold, so it nets and
        counts identically -- driven here by calling fulfill_hold directly with
        provider='test' (the same way that path does)."""
        hold = self._build_discounted_ga_hold(value="10")

        order = services.fulfill_hold(
            hold,
            buyer_email="buyer@example.com",
            buyer_name="Buyer",
            payment_ref="test-abc",
            provider="test",
        )

        self.assertEqual(order.total, Decimal("60.00"))
        self.assertEqual(order.discount_amount, Decimal("10.00"))
        self.assertEqual(order.promo_code_text, "TENOFF")
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.provider, "test")
        self.assertEqual(payment.amount, Decimal("60.00"))
        self.promo.refresh_from_db()
        self.assertEqual(self.promo.redemption_count, 1)

    def test_refund_reverses_the_net_amount(self):
        """A refund reverses what Stripe actually collected -- the NET total --
        so the refund Payment.amount == order.total (net), not the gross."""
        hold = self._build_discounted_ga_hold(value="10")
        order = services.fulfill_hold(
            hold,
            buyer_email="buyer@example.com",
            buyer_name="Buyer",
            payment_ref="test-abc",
            provider="test",  # no real charge -> refund records without calling Stripe
        )
        self.assertEqual(order.total, Decimal("60.00"))

        performed = services.refund_order(order)

        self.assertTrue(performed)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)
        refund_payment = Payment.objects.get(order=order, status="refunded")
        self.assertEqual(refund_payment.amount, Decimal("60.00"))  # net, == order.total

    def test_hold_without_promo_fulfills_with_zero_discount(self):
        self.build_ga_performance()
        hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        order, created = services.fulfill_checkout_session(self.org, self._session(hold))

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("70.00"))  # gross == net, unchanged
        self.assertEqual(order.discount_amount, Decimal("0.00"))
        self.assertEqual(order.promo_code_text, "")
