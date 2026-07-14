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

from donations.models import DonationCampaign
from events.models import Event, GAAllocation, Performance, PriceTier, PricingZone, ZoneTemplate
from orders import services as order_services
from orders.models import Hold, Order, OrderItem, Payment, Ticket
from orders.tests import OrdersFixtureMixin
from passes.models import PassProduct, PassPurchase, PassRedemption
from payments import services
from promotions.models import PromoCode
from venues.models import Seat
from venues.tests import make_org

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


# --- Donation add-on money path (line item, mixed fulfillment, standalone) ----
#
# The Phase 2 donation add-on: _line_items_for_hold appends one GROSS donation
# line on both GA and reserved paths; fulfill_hold records a kind=DONATION
# OrderItem alongside the tickets with a donation-inclusive Order.total (GA
# allocation bumped by tickets only); fulfill_donation turns a standalone
# (hold-less) gift into a null-performance paid Order; create_donation_checkout_
# session builds the standalone Stripe session; fulfill_checkout_session's
# donation metadata branch fulfills / replays / rejects malformed data; and a
# refund reverses a donation-only order cleanly.


def _add_donation(org, hold, *, amount="20", campaign=None):
    return order_services.set_hold_donation(
        organization=org,
        session_key=hold.session_key,
        hold_id=hold.pk,
        amount=amount,
        campaign=campaign,
    )


@override_settings(STRIPE_SECRET_KEY=PLATFORM_KEY)
class DonationLineItemTests(OrdersFixtureMixin, TestCase):
    """_line_items_for_hold appends exactly one GROSS donation line -- quantity
    1, correct minor units, name "Donation — <org name>" -- on BOTH the GA and
    reserved paths, and adds no extra line when the hold carries no gift."""

    def setUp(self):
        self.build_ga_performance()
        enable_connect(self.org)
        self.campaign = DonationCampaign.objects.create(organization=self.org)
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))

    def _ga_hold(self):
        return order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )

    def _line_items(self, hold):
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            services.create_checkout_session(hold, self.request)
            _, kwargs = mock_create.call_args
        return kwargs["line_items"]

    def test_no_donation_means_no_extra_line(self):
        line_items = self._line_items(self._ga_hold())
        self.assertEqual(len(line_items), 1)  # ticket line only

    def test_ga_path_appends_one_gross_donation_line(self):
        hold = self._ga_hold()
        hold = _add_donation(self.org, hold, amount="20", campaign=self.campaign)

        line_items = self._line_items(hold)

        self.assertEqual(len(line_items), 2)  # ticket + donation
        donation_line = line_items[-1]
        self.assertEqual(donation_line["quantity"], 1)
        self.assertEqual(donation_line["price_data"]["unit_amount"], 2000)  # $20.00 -> 2000
        self.assertIsInstance(donation_line["price_data"]["unit_amount"], int)
        self.assertEqual(donation_line["price_data"]["currency"], "usd")
        self.assertEqual(
            donation_line["price_data"]["product_data"]["name"],
            f"Donation — {self.org.name}",
        )

    def test_reserved_path_appends_one_gross_donation_line(self):
        # A fresh reserved org (build_reserved_performance would clash with the
        # GA "roxy" org already built in setUp, so use a separate one).
        from venues.models import SeatingChart, Section, Venue
        from events.models import Event, Performance, PriceTier

        org = make_org("reserved-org")
        enable_connect(org)
        venue = Venue.objects.create(organization=org, name="Hall")
        event = Event.objects.create(organization=org, title="Reserved Show", slug="rshow")
        perf = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        chart = SeatingChart.objects.create(organization=org, venue=venue, name="Standard")
        section = Section.objects.create(organization=org, chart=chart, name="Orchestra")
        seat = Seat.objects.create(organization=org, section=section, row_label="A", number="1")
        PriceTier.objects.create(
            organization=org, section=section, name="Orchestra", amount=Decimal("65.00")
        )
        campaign = DonationCampaign.objects.create(organization=org)
        hold = order_services.set_reserved_hold(
            organization=org,
            performance=perf,
            session_key="sess-r",
            user=None,
            seat_ids=[seat.id],
        )
        hold = order_services.set_hold_donation(
            organization=org, session_key="sess-r", hold_id=hold.pk, amount="15", campaign=campaign
        )
        request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(org.subdomain))

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            services.create_checkout_session(hold, request)
            _, kwargs = mock_create.call_args

        line_items = kwargs["line_items"]
        self.assertEqual(len(line_items), 2)  # one seat + donation
        donation_line = line_items[-1]
        self.assertEqual(donation_line["quantity"], 1)
        self.assertEqual(donation_line["price_data"]["unit_amount"], 1500)  # $15.00
        self.assertEqual(donation_line["price_data"]["product_data"]["name"], f"Donation — {org.name}")


class MixedTicketAndDonationFulfillmentTests(OrdersFixtureMixin, TestCase):
    """fulfill_hold on a cart with tickets AND a donation: N tickets + the
    ticket OrderItem + one kind=DONATION OrderItem; Order.total is donation-
    inclusive; GAAllocation.sold is bumped by the TICKETS only (a donation
    reserves no inventory); Payment.amount == total."""

    def setUp(self):
        self.build_ga_performance()  # 2 x $35 = $70 tickets
        self.campaign = DonationCampaign.objects.create(organization=self.org)
        self.hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        _add_donation(self.org, self.hold, amount="20", campaign=self.campaign)

    def _session(self, hold, session_id="cs_test_mixed"):
        return {
            "id": session_id,
            "payment_intent": "pi_test_mixed",
            "metadata": {"hold_id": str(hold.pk), "organization_id": str(hold.organization_id)},
            "customer_details": {"email": "buyer@example.com", "name": "Buyer Person"},
        }

    def test_mixed_order_shape_totals_and_inventory(self):
        order, created = services.fulfill_checkout_session(self.org, self._session(self.hold))

        self.assertTrue(created)
        # Donation-inclusive total: 2 x $35 + $20 gift = $90.
        self.assertEqual(order.total, Decimal("90.00"))
        self.assertEqual(Payment.objects.get(order=order).amount, Decimal("90.00"))

        # One ticket line + one donation line.
        self.assertEqual(order.items.count(), 2)
        ticket_item = order.items.get(kind=OrderItem.Kind.TICKET)
        self.assertEqual(ticket_item.quantity, 2)
        self.assertEqual(ticket_item.unit_amount, Decimal("35.00"))

        donation_item = order.items.get(kind=OrderItem.Kind.DONATION)
        self.assertEqual(donation_item.quantity, 1)
        self.assertEqual(donation_item.unit_amount, Decimal("20.00"))
        self.assertEqual(donation_item.donation_campaign_id, self.campaign.pk)
        self.assertIsNone(donation_item.seat_id)
        self.assertIsNone(donation_item.price_tier_id)

        # Two tickets minted (donation mints none), and GA sold bumped by 2 only.
        self.assertEqual(order.tickets.count(), 2)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 2)
        self.assertFalse(Hold.objects.filter(pk=self.hold.pk).exists())


@override_settings(STRIPE_SECRET_KEY=PLATFORM_KEY)
class PromoPlusDonationMoneyPathTests(OrdersFixtureMixin, TestCase):
    """Promo and donation together: the coupon's amount_off is the TICKET
    discount only, the application fee base is hold_grand_total (net tickets +
    donation), and Order.total reconciles at fulfillment."""

    def setUp(self):
        self.build_ga_performance()  # 2 x $35 = $70 ticket gross
        enable_connect(self.org)
        self.campaign = DonationCampaign.objects.create(organization=self.org)
        self.hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        _add_donation(self.org, self.hold, amount="20", campaign=self.campaign)
        PromoCode.objects.create(
            organization=self.org, code="TENOFF", kind=PromoCode.Kind.FIXED, value=Decimal("10")
        )
        self.hold = order_services.apply_promo_code(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk, code="TENOFF"
        )
        self.request = RequestFactory().post("/checkout/", HTTP_HOST=host_for(self.org.subdomain))

    @override_settings(PLATFORM_FEE_PERCENT=10, PLATFORM_FEE_FIXED_CENTS=0)
    @patch("payments.services.stripe.Coupon.create")
    @patch("payments.services.stripe.checkout.Session.create")
    def test_coupon_discounts_only_tickets_and_fee_is_on_the_net_grand_total(
        self, mock_session, mock_coupon
    ):
        mock_session.return_value = FakeStripeSession()
        mock_coupon.return_value = FakeCoupon()

        services.create_checkout_session(self.hold, self.request)

        # Coupon amount_off = the $10 TICKET discount only (never the gift).
        _, ckwargs = mock_coupon.call_args
        self.assertEqual(ckwargs["amount_off"], 1000)

        _, skwargs = mock_session.call_args
        # Two gross line items: $70 tickets + $20 donation.
        line_items = skwargs["line_items"]
        self.assertEqual(len(line_items), 2)
        self.assertEqual(line_items[0]["price_data"]["unit_amount"], 3500)  # gross ticket
        self.assertEqual(line_items[-1]["price_data"]["unit_amount"], 2000)  # gross donation
        # Fee base = hold_grand_total = (70 - 10) + 20 = $80 -> 10% -> 800 minor.
        self.assertEqual(skwargs["payment_intent_data"]["application_fee_amount"], 800)

    def test_fulfillment_total_reconciles_net_tickets_plus_donation(self):
        session = {
            "id": "cs_test_promo_donation",
            "payment_intent": "pi_test_pd",
            "metadata": {
                "hold_id": str(self.hold.pk),
                "organization_id": str(self.org.pk),
            },
            "customer_details": {"email": "buyer@example.com", "name": "Buyer"},
        }
        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("80.00"))  # (70 - 10) + 20
        self.assertEqual(order.discount_amount, Decimal("10.00"))
        self.assertEqual(order.promo_code_text, "TENOFF")
        self.assertEqual(Payment.objects.get(order=order).amount, Decimal("80.00"))
        donation_item = order.items.get(kind=OrderItem.Kind.DONATION)
        self.assertEqual(donation_item.unit_amount, Decimal("20.00"))


class FulfillDonationTests(OrdersFixtureMixin, TestCase):
    """fulfill_donation turns a standalone (hold-less) gift into a paid Order
    with performance=None, exactly one kind=DONATION OrderItem, a linked guest,
    and a succeeded Payment -- no tickets minted and no GA inventory touched."""

    def setUp(self):
        self.build_ga_performance()
        self.campaign = DonationCampaign.objects.create(organization=self.org)

    def test_creates_null_performance_paid_order_with_one_donation_item(self):
        order = services.fulfill_donation(
            self.org,
            amount=Decimal("50.00"),
            campaign=self.campaign,
            buyer_email="Donor@Example.com",
            buyer_name="Generous Donor",
            provider="test",
            payment_ref="test-donation-1",
        )

        self.assertIsNone(order.performance_id)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.total, Decimal("50.00"))
        self.assertEqual(order.buyer_email, "Donor@Example.com")

        # Exactly one donation item, no tickets.
        self.assertEqual(order.items.count(), 1)
        item = order.items.get()
        self.assertEqual(item.kind, OrderItem.Kind.DONATION)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.unit_amount, Decimal("50.00"))
        self.assertEqual(item.donation_campaign_id, self.campaign.pk)
        self.assertEqual(order.tickets.count(), 0)

        # Guest linked (email normalized) and a succeeded Payment recorded.
        self.assertIsNotNone(order.guest_id)
        self.assertEqual(order.guest.email, "donor@example.com")
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.amount, Decimal("50.00"))
        self.assertEqual(payment.provider, "test")

        # No GA inventory moved.
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 0)

    def test_campaign_may_be_none(self):
        order = services.fulfill_donation(
            self.org,
            amount=Decimal("15.00"),
            campaign=None,
            buyer_email="d@example.com",
            buyer_name="",
            provider="test",
            payment_ref="test-donation-2",
        )
        self.assertIsNone(order.items.get().donation_campaign_id)


@override_settings(STRIPE_SECRET_KEY=PLATFORM_KEY)
class CreateDonationCheckoutSessionTests(OrdersFixtureMixin, TestCase):
    """create_donation_checkout_session builds a standalone donation session:
    one line item at correct minor units, metadata carrying everything
    fulfillment needs (kind/amount/campaign/org), customer_email prefilled, the
    platform fee on the gift, and the session on the connected account."""

    def setUp(self):
        self.build_ga_performance()
        enable_connect(self.org)
        self.campaign = DonationCampaign.objects.create(organization=self.org)
        self.request = RequestFactory().post("/donate/", HTTP_HOST=host_for(self.org.subdomain))

    def _create(self, **overrides):
        kwargs = dict(
            amount=Decimal("30.00"),
            campaign=self.campaign,
            buyer_email="donor@example.com",
            buyer_name="Donor",
            request=self.request,
        )
        kwargs.update(overrides)
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            url = services.create_donation_checkout_session(self.org, **kwargs)
            _, call_kwargs = mock_create.call_args
        return url, call_kwargs

    def test_single_line_item_metadata_email_and_connected_account(self):
        url, kwargs = self._create()

        self.assertEqual(url, "https://checkout.stripe.com/pay/cs_test_123")
        # Direct charge on the connected account with the platform key.
        self.assertEqual(kwargs["api_key"], PLATFORM_KEY)
        self.assertEqual(kwargs["stripe_account"], TEST_ACCT)
        self.assertEqual(kwargs["mode"], "payment")

        line_items = kwargs["line_items"]
        self.assertEqual(len(line_items), 1)
        item = line_items[0]
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["price_data"]["unit_amount"], 3000)  # $30.00 -> 3000
        self.assertIsInstance(item["price_data"]["unit_amount"], int)
        self.assertEqual(item["price_data"]["product_data"]["name"], f"Donation — {self.org.name}")

        self.assertEqual(kwargs["metadata"]["kind"], "donation")
        self.assertEqual(kwargs["metadata"]["organization_id"], str(self.org.pk))
        self.assertEqual(kwargs["metadata"]["donation_campaign_id"], str(self.campaign.pk))
        self.assertEqual(kwargs["metadata"]["donation_amount"], "30.00")
        self.assertEqual(kwargs["customer_email"], "donor@example.com")

    def test_no_campaign_leaves_blank_campaign_id_metadata(self):
        _, kwargs = self._create(campaign=None)
        self.assertEqual(kwargs["metadata"]["donation_campaign_id"], "")

    def test_no_buyer_email_omits_customer_email(self):
        _, kwargs = self._create(buyer_email="")
        self.assertNotIn("customer_email", kwargs)

    @override_settings(PLATFORM_FEE_PERCENT=10, PLATFORM_FEE_FIXED_CENTS=0)
    def test_application_fee_is_charged_on_the_gift_amount(self):
        _, kwargs = self._create(amount=Decimal("30.00"))
        # 10% of $30 -> $3.00 -> 300 minor units.
        self.assertEqual(kwargs["payment_intent_data"]["application_fee_amount"], 300)

    def test_no_fee_when_rate_is_zero(self):
        _, kwargs = self._create()
        self.assertNotIn("payment_intent_data", kwargs)

    def test_per_org_fee_override_wins(self):
        self.org.platform_fee_percent = Decimal("5")
        self.org.save(update_fields=["platform_fee_percent"])
        _, kwargs = self._create(amount=Decimal("30.00"))
        # 5% of $30 -> 150 minor units.
        self.assertEqual(kwargs["payment_intent_data"]["application_fee_amount"], 150)


class FulfillDonationCheckoutSessionTests(OrdersFixtureMixin, TestCase):
    """fulfill_checkout_session's donation metadata branch: creates via
    fulfill_donation, replays idempotently, rejects missing/garbled
    donation_amount with DonationDataError (writing nothing), and still enforces
    the tenant-mismatch check."""

    def setUp(self):
        self.build_ga_performance()
        self.campaign = DonationCampaign.objects.create(organization=self.org)

    def _session(self, *, session_id="cs_don", amount="45.00", campaign_id=None, org_id=None, email="donor@example.com"):
        metadata = {
            "kind": "donation",
            "organization_id": str(self.org.pk if org_id is None else org_id),
            "donation_amount": amount,
        }
        if campaign_id is not None:
            metadata["donation_campaign_id"] = campaign_id
        return {
            "id": session_id,
            "payment_intent": "pi_don",
            "metadata": metadata,
            "customer_details": {"email": email, "name": "Donor"},
        }

    def test_creates_a_donation_order_from_metadata(self):
        session = self._session(amount="45.00", campaign_id=str(self.campaign.pk))

        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertIsNone(order.performance_id)
        self.assertEqual(order.total, Decimal("45.00"))
        self.assertEqual(order.status, Order.Status.PAID)
        item = order.items.get()
        self.assertEqual(item.kind, OrderItem.Kind.DONATION)
        self.assertEqual(item.unit_amount, Decimal("45.00"))
        self.assertEqual(item.donation_campaign_id, self.campaign.pk)
        self.assertEqual(order.tickets.count(), 0)
        self.assertEqual(order.stripe_checkout_session_id, "cs_don")
        # payment_ref is the PaymentIntent when present.
        self.assertEqual(Payment.objects.get(order=order).provider_ref, "pi_don")

    def test_replaying_the_same_donation_session_is_idempotent(self):
        session = self._session(campaign_id=str(self.campaign.pk))

        order1, created1 = services.fulfill_checkout_session(self.org, session)
        order2, created2 = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(order1.pk, order2.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)

    def test_missing_donation_amount_raises_and_writes_nothing(self):
        session = self._session()
        del session["metadata"]["donation_amount"]

        with self.assertRaises(services.DonationDataError):
            services.fulfill_checkout_session(self.org, session)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_garbled_donation_amount_raises_and_writes_nothing(self):
        session = self._session(amount="not-a-number")

        with self.assertRaises(services.DonationDataError):
            services.fulfill_checkout_session(self.org, session)
        self.assertEqual(Order.objects.count(), 0)

    def test_donation_data_error_is_a_fulfillment_error(self):
        # The webhook view acks 200 on any FulfillmentError; DonationDataError
        # must be one so a session that can never heal isn't retried for 3 days.
        self.assertTrue(issubclass(services.DonationDataError, services.FulfillmentError))

    def test_deleted_campaign_still_fulfills_with_null_campaign_fk(self):
        session = self._session(campaign_id=str(self.campaign.pk))
        self.campaign.delete()

        order, created = services.fulfill_checkout_session(self.org, session)

        self.assertTrue(created)
        self.assertEqual(order.total, Decimal("45.00"))
        self.assertIsNone(order.items.get().donation_campaign_id)

    def test_tenant_mismatch_still_applies_to_a_donation_session(self):
        other_org = make_org("org-mismatch")
        session = self._session(org_id=other_org.pk)

        with self.assertRaises(services.TenantMismatchError):
            services.fulfill_checkout_session(self.org, session)
        self.assertEqual(Order.objects.count(), 0)


class RefundDonationOnlyOrderTests(OrdersFixtureMixin, TestCase):
    """refund_order on a paid donation-only order (no tickets, null
    performance): succeeds, void_order returns 0 (nothing to void, no GA
    decrement), status flips to REFUNDED, and the refund Payment.amount ==
    order.total."""

    def setUp(self):
        self.build_ga_performance()
        self.campaign = DonationCampaign.objects.create(organization=self.org)

    def test_refund_of_a_donation_only_order(self):
        order = services.fulfill_donation(
            self.org,
            amount=Decimal("50.00"),
            campaign=self.campaign,
            buyer_email="donor@example.com",
            buyer_name="Donor",
            provider="test",  # no real charge -> refund records without calling Stripe
            payment_ref="test-donation",
        )

        # void_order itself is a no-op returning 0 on this ticketless order.
        self.assertEqual(order_services.void_order(order), 0)

        performed = services.refund_order(order)

        self.assertTrue(performed)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)
        refund_payment = Payment.objects.get(order=order, status="refunded")
        self.assertEqual(refund_payment.amount, Decimal("50.00"))  # == order.total
        # No GA inventory was ever involved.
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 0)


# --- Phase 3: pass purchase + redemption money path ---------------------------
#
# fulfill_pass_purchase turns a paid one-time pass SALE into a null-performance
# paid Order + kind=PASS OrderItem + the PassPurchase entitlement (every term
# snapshotted) + a Payment. create_pass_checkout_session builds the standalone
# Stripe session; fulfill_checkout_session's kind="pass" branch fulfills /
# replays / rejects missing/cross-org products. fulfill_hold_with_pass is the
# REDEMPTION core -- it spends a pass on held seats for a $0 order, minting real
# tickets against real inventory and recording a PassRedemption per ticket. And
# refund_order guards a used pass, reverses a redemption's entitlement, and
# handles the $0 (provider="pass") redemption order without calling Stripe.

FLEX = PassProduct.Kind.FLEX
SEASON = PassProduct.Kind.SEASON
PASS_ACTIVE = PassPurchase.Status.ACTIVE
PASS_EXHAUSTED = PassPurchase.Status.EXHAUSTED
PASS_REFUNDED = PassPurchase.Status.REFUNDED


class PassMoneyPathMixin(OrdersFixtureMixin):
    """Helpers to mint a pass product + a live PassPurchase on self.org and a
    second GA event/performance in the same org (for season multi-event tests)."""

    def _flex_product(self, *, credit_count=4, price="120.00", name="Flex 4"):
        return PassProduct.objects.create(
            organization=self.org, name=name, kind=FLEX,
            price=Decimal(price), credit_count=credit_count,
        )

    def _season_product(self, *, price="200.00", name="Season", events=()):
        product = PassProduct.objects.create(
            organization=self.org, name=name, kind=SEASON, price=Decimal(price)
        )
        if events:
            product.events.set(events)
        return product

    def _purchase(self, product, *, credits_remaining=None, status=PASS_ACTIVE,
                  valid_from=None, valid_until=None, covered_events=(), guest=None):
        order = Order.objects.create(
            organization=self.org, buyer_email="holder@example.com",
            total=product.price, status=Order.Status.PAID,
        )
        purchase = PassPurchase.objects.create(
            organization=self.org, product=product, order=order, guest=guest,
            kind=product.kind, credit_count=product.credit_count,
            credits_remaining=(product.credit_count if credits_remaining is None else credits_remaining),
            valid_from=valid_from, valid_until=valid_until, status=status,
        )
        if covered_events:
            purchase.covered_events.set(covered_events)
        return purchase

    def _second_ga_event(self, *, slug="show2", title="Show 2"):
        event = Event.objects.create(organization=self.org, title=title, slug=slug)
        perf = Performance.objects.create(
            organization=self.org, event=event, venue=self.venue,
            starts_at=timezone.now(), seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org, performance=perf, capacity=100)
        tier = PriceTier.objects.create(
            organization=self.org, performance=perf, name="GA", amount=Decimal("35.00")
        )
        return event, perf, tier

    def _ga_hold(self, *, quantity=1, performance=None, price_tier=None, session_key="sess-redeem"):
        return order_services.set_ga_hold(
            organization=self.org,
            performance=performance or self.performance,
            session_key=session_key,
            user=None,
            price_tier=price_tier or self.price_tier,
            quantity=quantity,
        )


class FulfillPassPurchaseTests(PassMoneyPathMixin, TestCase):
    """fulfill_pass_purchase: the SALE path (not the redemption path)."""

    def setUp(self):
        self.build_ga_performance()
        self.event_a = self.event  # "show"
        self.event_b, _, _ = self._second_ga_event()

    def _fulfill(self, product, **kw):
        defaults = dict(
            buyer_email="Buyer@Example.com", buyer_name="Buyer",
            provider="test", payment_ref="test-pass-1",
        )
        defaults.update(kw)
        return services.fulfill_pass_purchase(self.org, product=product, **defaults)

    def test_flex_sale_creates_order_item_purchase_and_payment(self):
        product = self._flex_product(credit_count=4, price="120.00")
        order = self._fulfill(product)

        self.assertIsNone(order.performance_id)  # a pass reserves no performance
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.total, Decimal("120.00"))

        item = order.items.get()
        self.assertEqual(item.kind, OrderItem.Kind.PASS)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.unit_amount, Decimal("120.00"))
        self.assertEqual(item.pass_product_id, product.pk)
        self.assertEqual(order.tickets.count(), 0)

        purchase = order.pass_purchases.get()
        self.assertEqual(purchase.kind, FLEX)
        self.assertEqual(purchase.credit_count, 4)
        self.assertEqual(purchase.credits_remaining, 4)  # starts at a full balance
        self.assertEqual(purchase.status, PASS_ACTIVE)

        # Guest linked (email normalized), and a succeeded Payment recorded.
        self.assertIsNotNone(order.guest_id)
        self.assertEqual(order.guest.email, "buyer@example.com")
        self.assertEqual(purchase.guest_id, order.guest_id)
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.provider, "test")
        self.assertEqual(payment.amount, Decimal("120.00"))
        self.assertEqual(payment.status, "succeeded")

    def test_season_sale_snapshots_window_and_covered_events(self):
        vf = timezone.now() - timedelta(days=5)
        vu = timezone.now() + timedelta(days=30)
        product = self._season_product(events=[self.event_a])
        product.valid_from = vf
        product.valid_until = vu
        product.save(update_fields=["valid_from", "valid_until"])

        order = self._fulfill(product)
        purchase = order.pass_purchases.get()

        self.assertEqual(purchase.kind, SEASON)
        self.assertIsNone(purchase.credit_count)
        self.assertIsNone(purchase.credits_remaining)
        self.assertEqual(purchase.valid_from, vf)
        self.assertEqual(purchase.valid_until, vu)
        self.assertEqual(set(purchase.covered_events.all()), {self.event_a})

    def test_later_product_edit_does_not_change_the_sold_purchase(self):
        product = self._season_product(events=[self.event_a])
        order = self._fulfill(product)
        purchase = order.pass_purchases.get()

        # Edit the product AFTER the sale: add a covered event, change price,
        # narrow the window. None of it may touch the snapshotted purchase.
        product.events.add(self.event_b)
        product.price = Decimal("999.00")
        product.valid_until = timezone.now() - timedelta(days=1)
        product.save(update_fields=["price", "valid_until"])

        purchase.refresh_from_db()
        self.assertEqual(set(purchase.covered_events.all()), {self.event_a})
        self.assertIsNone(purchase.valid_until)
        self.assertEqual(order.items.get().unit_amount, Decimal("200.00"))

    def test_inactive_product_raises_pass_data_error(self):
        product = self._flex_product()
        product.is_active = False
        product.save(update_fields=["is_active"])

        with self.assertRaises(services.PassDataError):
            self._fulfill(product)
        self.assertFalse(PassPurchase.objects.exists())


@override_settings(STRIPE_SECRET_KEY=PLATFORM_KEY)
class CreatePassCheckoutSessionTests(PassMoneyPathMixin, TestCase):
    """create_pass_checkout_session builds a standalone pass session: one line
    item at the pass price, metadata carrying kind/pass_product_id/org, the
    platform fee on the price, customer_email prefilled, on the connected acct."""

    def setUp(self):
        self.build_ga_performance()
        enable_connect(self.org)
        self.product = self._flex_product(credit_count=4, price="120.00")
        self.request = RequestFactory().post("/passes/buy/", HTTP_HOST=host_for(self.org.subdomain))

    def _create(self, **overrides):
        kwargs = dict(
            product=self.product, buyer_email="buyer@example.com",
            buyer_name="Buyer", request=self.request,
        )
        kwargs.update(overrides)
        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = FakeStripeSession()
            url = services.create_pass_checkout_session(self.org, **kwargs)
            _, call_kwargs = mock_create.call_args
        return url, call_kwargs

    def test_line_item_metadata_email_and_connected_account(self):
        url, kwargs = self._create()

        self.assertEqual(url, "https://checkout.stripe.com/pay/cs_test_123")
        self.assertEqual(kwargs["api_key"], PLATFORM_KEY)
        self.assertEqual(kwargs["stripe_account"], TEST_ACCT)
        self.assertEqual(kwargs["mode"], "payment")

        line_items = kwargs["line_items"]
        self.assertEqual(len(line_items), 1)
        item = line_items[0]
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["price_data"]["unit_amount"], 12000)  # $120.00 -> minor units
        self.assertIsInstance(item["price_data"]["unit_amount"], int)
        self.assertIn(self.product.name, item["price_data"]["product_data"]["name"])

        self.assertEqual(kwargs["metadata"]["kind"], "pass")
        self.assertEqual(kwargs["metadata"]["pass_product_id"], str(self.product.pk))
        self.assertEqual(kwargs["metadata"]["organization_id"], str(self.org.pk))
        self.assertEqual(kwargs["customer_email"], "buyer@example.com")

    def test_no_buyer_email_omits_customer_email(self):
        _, kwargs = self._create(buyer_email="")
        self.assertNotIn("customer_email", kwargs)

    @override_settings(PLATFORM_FEE_PERCENT=10, PLATFORM_FEE_FIXED_CENTS=0)
    def test_application_fee_is_charged_on_the_pass_price(self):
        _, kwargs = self._create()
        # 10% of $120 -> $12.00 -> 1200 minor units.
        self.assertEqual(kwargs["payment_intent_data"]["application_fee_amount"], 1200)

    def test_no_fee_when_rate_is_zero(self):
        _, kwargs = self._create()
        self.assertNotIn("payment_intent_data", kwargs)

    # NOTE: the STUB-mode branch (charges not enabled -> reverse("pass_stub"))
    # is intentionally NOT tested here: the `pass_stub` route lands with the
    # passes UI layer (passes/urls.py, owned by the UI agent and not present in
    # this service-layer slice), and the spec scopes create_pass_checkout_session
    # coverage to the charges-enabled Stripe path.


class FulfillPassCheckoutSessionWebhookTests(PassMoneyPathMixin, TestCase):
    """fulfill_checkout_session's kind="pass" metadata branch: creates via
    fulfill_pass_purchase, replays idempotently, and rejects a missing/cross-org
    product with PassDataError (writing nothing)."""

    def setUp(self):
        self.build_ga_performance()
        self.product = self._flex_product(credit_count=4, price="120.00")

    def _session(self, *, session_id="cs_pass", product_id=None, org_id=None, email="buyer@example.com"):
        return {
            "id": session_id,
            "payment_intent": "pi_pass",
            "metadata": {
                "kind": "pass",
                "organization_id": str(self.org.pk if org_id is None else org_id),
                "pass_product_id": str(self.product.pk if product_id is None else product_id),
            },
            "customer_details": {"email": email, "name": "Buyer"},
        }

    def test_creates_a_pass_purchase_from_metadata(self):
        order, created = services.fulfill_checkout_session(self.org, self._session())

        self.assertTrue(created)
        self.assertIsNone(order.performance_id)
        self.assertEqual(order.total, Decimal("120.00"))
        self.assertEqual(order.stripe_checkout_session_id, "cs_pass")
        purchase = order.pass_purchases.get()
        self.assertEqual(purchase.kind, FLEX)
        self.assertEqual(purchase.credits_remaining, 4)
        self.assertEqual(Payment.objects.get(order=order).provider_ref, "pi_pass")

    def test_replaying_the_same_pass_session_is_idempotent(self):
        order1, created1 = services.fulfill_checkout_session(self.org, self._session())
        order2, created2 = services.fulfill_checkout_session(self.org, self._session())

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(order1.pk, order2.pk)
        self.assertEqual(Order.objects.filter(stripe_checkout_session_id="cs_pass").count(), 1)
        self.assertEqual(PassPurchase.objects.count(), 1)

    def test_missing_product_raises_and_writes_nothing(self):
        session = self._session()
        del session["metadata"]["pass_product_id"]

        with self.assertRaises(services.PassDataError):
            services.fulfill_checkout_session(self.org, session)
        self.assertEqual(Order.objects.filter(stripe_checkout_session_id="cs_pass").count(), 0)
        self.assertFalse(PassPurchase.objects.exists())

    def test_cross_org_product_raises_and_writes_nothing(self):
        other_org = make_org("other-pass-org")
        other_product = PassProduct.objects.create(
            organization=other_org, name="Theirs", kind=FLEX,
            price=Decimal("50.00"), credit_count=2,
        )
        # A product that exists but belongs to another org isn't found under the
        # org-scoped lookup -> PassDataError, nothing issued for this org.
        session = self._session(product_id=other_product.pk)

        with self.assertRaises(services.PassDataError):
            services.fulfill_checkout_session(self.org, session)
        self.assertFalse(PassPurchase.objects.filter(organization=self.org).exists())


class FulfillHoldWithPassFlexTests(PassMoneyPathMixin, TestCase):
    """The REDEMPTION core on a FLEX pass over a GA performance."""

    def setUp(self):
        self.build_ga_performance()  # $35 GA tier, capacity 100

    def test_flex_ga_redemption_mints_tickets_and_burns_credits(self):
        purchase = self._purchase(self._flex_product(credit_count=2))
        hold = self._ga_hold(quantity=2)

        order = services.fulfill_hold_with_pass(
            hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )

        # $0 PAID order tied to the performance.
        self.assertEqual(order.total, Decimal("0.00"))
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.performance_id, self.performance.pk)

        # Ticket OrderItem at face value; two tickets minted; GA sold bumped.
        item = order.items.get()
        self.assertEqual(item.kind, OrderItem.Kind.TICKET)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.unit_amount, Decimal("35.00"))
        self.assertEqual(order.tickets.count(), 2)
        self.assertTrue(all(t.seat_id is None for t in order.tickets.all()))
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 2)

        # Two PassRedemptions, one credit each, face value snapshotted.
        redemptions = list(order.pass_redemptions.all())
        self.assertEqual(len(redemptions), 2)
        self.assertTrue(all(r.credits_used == 1 for r in redemptions))
        self.assertTrue(all(r.face_value == Decimal("35.00") for r in redemptions))
        self.assertTrue(all(r.event_id == self.event.pk for r in redemptions))

        # Credits decremented to 0 -> EXHAUSTED; a $0 pass Payment; hold gone.
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 0)
        self.assertEqual(purchase.status, PASS_EXHAUSTED)
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.provider, "pass")
        self.assertEqual(payment.amount, Decimal("0.00"))
        self.assertFalse(Hold.objects.filter(pk=hold.pk).exists())

    def test_flex_partial_redemption_leaves_pass_active(self):
        purchase = self._purchase(self._flex_product(credit_count=4))
        hold = self._ga_hold(quantity=1)

        services.fulfill_hold_with_pass(
            hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )

        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 3)
        self.assertEqual(purchase.status, PASS_ACTIVE)

    def test_over_redeem_raises_and_writes_nothing(self):
        purchase = self._purchase(self._flex_product(credit_count=1))
        hold = self._ga_hold(quantity=2)  # wants 2, only 1 credit

        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )

        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PassRedemption.objects.count(), 0)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 0)
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 1)  # untouched
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())

    def test_performance_outside_window_raises(self):
        purchase = self._purchase(
            self._flex_product(credit_count=2),
            valid_from=timezone.now() + timedelta(days=1),  # perf starts now -> not covered
        )
        hold = self._ga_hold(quantity=1)
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())

    def test_expired_pass_raises(self):
        purchase = self._purchase(
            self._flex_product(credit_count=2),
            valid_until=timezone.now() - timedelta(minutes=1),
        )
        hold = self._ga_hold(quantity=1)
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        self.assertEqual(PassRedemption.objects.count(), 0)

    def test_refunded_pass_raises(self):
        purchase = self._purchase(self._flex_product(credit_count=2), status=PASS_REFUNDED)
        hold = self._ga_hold(quantity=1)
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        self.assertEqual(PassRedemption.objects.count(), 0)

    def test_hold_with_promo_is_rejected_and_writes_nothing(self):
        purchase = self._purchase(self._flex_product(credit_count=2))
        hold = self._ga_hold(quantity=1)
        PromoCode.objects.create(
            organization=self.org, code="TENOFF", kind=PromoCode.Kind.FIXED, value=Decimal("10")
        )
        hold = order_services.apply_promo_code(
            organization=self.org, session_key=hold.session_key, hold_id=hold.pk, code="TENOFF"
        )

        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        self.assertEqual(PassRedemption.objects.count(), 0)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())

    def test_hold_with_donation_is_rejected_and_writes_nothing(self):
        purchase = self._purchase(self._flex_product(credit_count=2))
        hold = self._ga_hold(quantity=1)
        hold = order_services.set_hold_donation(
            organization=self.org, session_key=hold.session_key, hold_id=hold.pk,
            amount="15", campaign=None,
        )

        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        self.assertEqual(PassRedemption.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())

    def test_expired_hold_raises_hold_gone(self):
        purchase = self._purchase(self._flex_product(credit_count=2))
        hold = self._ga_hold(quantity=1)
        hold.expires_at = timezone.now() - timedelta(minutes=1)
        hold.save(update_fields=["expires_at"])

        with self.assertRaises(services.HoldGoneError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        self.assertEqual(PassRedemption.objects.count(), 0)

    def test_double_spend_committed_state_second_call_rejected(self):
        """Two SEQUENTIAL redemptions of a 1-credit flex pass: the first
        commits (credits -> 0, EXHAUSTED), so the second is rejected by the
        committed state. This is the same committed-state pattern the existing
        Stripe idempotency-race tests use; a true multi-connection race would
        need the multiprocess harness (orders/test_concurrency_multiprocess.py),
        but the season backstop DB constraint that guards the true-race case is
        covered directly in passes/test_services.py
        (SeasonEventRedemptionConstraintTests) and the flex balance is a locked
        select_for_update decrement, so the committed-state coverage here is
        sufficient for this suite."""
        purchase = self._purchase(self._flex_product(credit_count=1))
        first_hold = self._ga_hold(quantity=1, session_key="sess-first")

        services.fulfill_hold_with_pass(
            first_hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PASS_EXHAUSTED)

        second_hold = self._ga_hold(quantity=1, session_key="sess-second")
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                second_hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        # Only the first redemption exists.
        self.assertEqual(PassRedemption.objects.count(), 1)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 1)


class FulfillHoldWithPassReservedTests(PassMoneyPathMixin, TestCase):
    """A FLEX redemption over a RESERVED performance snapshots each seat onto its
    redemption row, and an availability race surfaces as AvailabilityChangedError."""

    def setUp(self):
        self.build_reserved_performance()  # $65 section default, one seat A1
        self.second_seat = Seat.objects.create(
            organization=self.org, section=self.section, row_label="A", number="2"
        )

    def _reserved_hold(self, seat_ids, session_key="sess-reserved"):
        return order_services.set_reserved_hold(
            organization=self.org, performance=self.performance,
            session_key=session_key, user=None, seat_ids=seat_ids,
        )

    def test_reserved_redemption_snapshots_seats_into_redemption_rows(self):
        purchase = self._purchase(self._flex_product(credit_count=2))
        hold = self._reserved_hold([self.seat.id, self.second_seat.id])

        order = services.fulfill_hold_with_pass(
            hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )

        self.assertEqual(order.total, Decimal("0.00"))
        self.assertEqual(order.tickets.count(), 2)
        redemptions = list(order.pass_redemptions.all())
        self.assertEqual({r.seat_id for r in redemptions}, {self.seat.id, self.second_seat.id})
        self.assertTrue(all(r.credits_used == 1 for r in redemptions))
        self.assertTrue(all(r.face_value == Decimal("65.00") for r in redemptions))
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 0)
        self.assertEqual(purchase.status, PASS_EXHAUSTED)

    def test_seat_ticketed_between_hold_and_redeem_raises_availability_changed(self):
        purchase = self._purchase(self._flex_product(credit_count=2))
        hold = self._reserved_hold([self.seat.id])
        # The seat gets ticketed by another order after the hold, before redeem.
        other_order = Order.objects.create(
            organization=self.org, performance=self.performance,
            buyer_email="other@example.com", total=Decimal("65.00"), status=Order.Status.PAID,
        )
        Ticket.objects.create(
            organization=self.org, order=other_order, performance=self.performance, seat=self.seat
        )

        with self.assertRaises(services.AvailabilityChangedError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
            )
        # Nothing consumed: no redemption rows and the credit balance is intact.
        self.assertEqual(PassRedemption.objects.count(), 0)
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 2)


class FulfillHoldWithPassSeasonTests(PassMoneyPathMixin, TestCase):
    """The REDEMPTION core on a SEASON pass: one admission per covered event."""

    def setUp(self):
        self.build_ga_performance()  # event "show" == self.event, self.performance
        self.event_a = self.event
        self.perf_a = self.performance
        self.tier_a = self.price_tier
        self.event_b, self.perf_b, self.tier_b = self._second_ga_event()

    def _season(self, events):
        return self._purchase(self._season_product(events=events), covered_events=events)

    def test_single_covered_event_redeems_with_zero_credits(self):
        purchase = self._season([self.event_a, self.event_b])
        hold = self._ga_hold(quantity=1, performance=self.perf_a, price_tier=self.tier_a)

        order = services.fulfill_hold_with_pass(
            hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )

        redemption = order.pass_redemptions.get()
        self.assertEqual(redemption.credits_used, 0)
        self.assertEqual(redemption.event_id, self.event_a.pk)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PASS_ACTIVE)  # season never EXHAUSTED
        self.assertIsNone(purchase.credits_remaining)

    def test_second_redemption_same_event_is_rejected(self):
        purchase = self._season([self.event_a])
        first = self._ga_hold(quantity=1, performance=self.perf_a, price_tier=self.tier_a, session_key="s1")
        services.fulfill_hold_with_pass(first, purchase, buyer_email="h@example.com", buyer_name="Holder")

        second = self._ga_hold(quantity=1, performance=self.perf_a, price_tier=self.tier_a, session_key="s2")
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(second, purchase, buyer_email="h@example.com", buyer_name="Holder")
        self.assertEqual(purchase.redemptions.count(), 1)

    def test_different_covered_event_redeems_ok(self):
        purchase = self._season([self.event_a, self.event_b])
        first = self._ga_hold(quantity=1, performance=self.perf_a, price_tier=self.tier_a, session_key="s1")
        services.fulfill_hold_with_pass(first, purchase, buyer_email="h@example.com", buyer_name="Holder")

        second = self._ga_hold(quantity=1, performance=self.perf_b, price_tier=self.tier_b, session_key="s2")
        services.fulfill_hold_with_pass(second, purchase, buyer_email="h@example.com", buyer_name="Holder")

        self.assertEqual(purchase.redemptions.count(), 2)
        self.assertEqual(
            {r.event_id for r in purchase.redemptions.all()}, {self.event_a.pk, self.event_b.pk}
        )

    def test_quantity_greater_than_one_is_rejected(self):
        purchase = self._season([self.event_a])
        hold = self._ga_hold(quantity=2, performance=self.perf_a, price_tier=self.tier_a)
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(hold, purchase, buyer_email="h@example.com", buyer_name="Holder")
        self.assertEqual(PassRedemption.objects.count(), 0)

    def test_uncovered_event_is_rejected(self):
        purchase = self._season([self.event_a])  # covers A only
        hold = self._ga_hold(quantity=1, performance=self.perf_b, price_tier=self.tier_b)
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(hold, purchase, buyer_email="h@example.com", buyer_name="Holder")
        self.assertEqual(PassRedemption.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())


class RefundPassPurchaseOrderTests(PassMoneyPathMixin, TestCase):
    """refund_order on a pass PURCHASE (sale) order: blocked while the pass has
    redemptions; otherwise refunds and marks every PassPurchase REFUNDED."""

    def setUp(self):
        self.build_ga_performance()

    def _sell(self, product):
        return services.fulfill_pass_purchase(
            self.org, product=product, buyer_email="holder@example.com",
            buyer_name="Holder", provider="test", payment_ref="test-pass-sale",
        )

    def test_refund_blocked_while_pass_has_redemptions(self):
        sale_order = self._sell(self._flex_product(credit_count=2))
        purchase = sale_order.pass_purchases.get()
        # Redeem one credit so the pass now has a redemption.
        hold = self._ga_hold(quantity=1)
        redemption_order = services.fulfill_hold_with_pass(
            hold, purchase, buyer_email="holder@example.com", buyer_name="Holder"
        )

        with self.assertRaises(services.RefundError):
            services.refund_order(sale_order)

        sale_order.refresh_from_db()
        self.assertEqual(sale_order.status, Order.Status.PAID)  # unchanged
        purchase.refresh_from_db()
        self.assertNotEqual(purchase.status, PASS_REFUNDED)
        # The redeemed ticket is NOT voided by the rejected refund.
        self.assertTrue(
            redemption_order.tickets.exclude(status=Ticket.Status.VOID).exists()
        )

    def test_refund_of_an_unredeemed_purchase_marks_it_refunded(self):
        sale_order = self._sell(self._flex_product(credit_count=2))
        purchase = sale_order.pass_purchases.get()

        performed = services.refund_order(sale_order)

        self.assertTrue(performed)
        sale_order.refresh_from_db()
        self.assertEqual(sale_order.status, Order.Status.REFUNDED)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PASS_REFUNDED)

        # A refunded pass can no longer be redeemed.
        hold = self._ga_hold(quantity=1)
        with self.assertRaises(services.PassRedemptionError):
            services.fulfill_hold_with_pass(
                hold, purchase, buyer_email="holder@example.com", buyer_name="Holder"
            )


class RefundPassRedemptionOrderTests(PassMoneyPathMixin, TestCase):
    """refund_order on a pass REDEMPTION order ($0, provider="pass"): voids the
    tickets, deletes the PassRedemptions, gives the entitlement back, records a
    $0 refund Payment, and calls no Stripe (nothing was charged)."""

    def setUp(self):
        self.build_ga_performance()

    def test_flex_redemption_refund_restores_credits_and_records_zero(self):
        purchase = self._purchase(self._flex_product(credit_count=2))
        hold = self._ga_hold(quantity=1)
        redemption_order = services.fulfill_hold_with_pass(
            hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 1)

        with patch("payments.services.stripe.Refund.create") as mock_refund:
            performed = services.refund_order(redemption_order)
            mock_refund.assert_not_called()  # provider="pass", nothing charged

        self.assertTrue(performed)
        redemption_order.refresh_from_db()
        self.assertEqual(redemption_order.status, Order.Status.REFUNDED)
        # Tickets voided; redemption rows deleted; GA inventory freed.
        self.assertFalse(
            redemption_order.tickets.exclude(status=Ticket.Status.VOID).exists()
        )
        self.assertEqual(PassRedemption.objects.count(), 0)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 0)
        # Flex credit restored.
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 2)
        # The refund Payment is $0 for a redemption order.
        refund_payment = Payment.objects.get(order=redemption_order, status="refunded")
        self.assertEqual(refund_payment.amount, Decimal("0.00"))

        # And the freed credit can be spent again.
        hold2 = self._ga_hold(quantity=1, session_key="sess-again")
        services.fulfill_hold_with_pass(
            hold2, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 1)

    def test_season_redemption_refund_frees_the_event_slot(self):
        product = self._season_product(events=[self.event])
        purchase = self._purchase(product, covered_events=[self.event])
        hold = self._ga_hold(quantity=1)
        redemption_order = services.fulfill_hold_with_pass(
            hold, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )
        self.assertEqual(purchase.redemptions.count(), 1)

        performed = services.refund_order(redemption_order)
        self.assertTrue(performed)
        self.assertEqual(purchase.redemptions.count(), 0)  # slot freed

        # The same covered event can be redeemed again now the slot is free.
        hold2 = self._ga_hold(quantity=1, session_key="sess-reagain")
        services.fulfill_hold_with_pass(
            hold2, purchase, buyer_email="h@example.com", buyer_name="Holder"
        )
        self.assertEqual(purchase.redemptions.count(), 1)


class SendOrderReceiptPassDispatchTests(PassMoneyPathMixin, TestCase):
    """send_order_receipt routes a pass-only order (no tickets, a kind=PASS line)
    to the pass receipt email, NOT the donation fallback."""

    def setUp(self):
        self.build_ga_performance()

    def test_pass_only_order_sends_pass_email_not_donation(self):
        from django.core import mail
        from orders import emails

        product = self._flex_product(credit_count=4, name="Season Flex")
        order = services.fulfill_pass_purchase(
            self.org, product=product, buyer_email="buyer@example.com",
            buyer_name="Buyer", provider="test", payment_ref="test-pass",
        )
        request = RequestFactory().post("/", HTTP_HOST=host_for(self.org.subdomain))

        emails.send_order_receipt(order, request)

        self.assertEqual(len(mail.outbox), 1)
        subject = mail.outbox[0].subject
        self.assertIn(product.name, subject)
        self.assertNotIn("donation", subject.lower())
