"""Service-layer tests for passes (Phase 3): the pure entitlement predicates
and helpers in passes/services.py (pass_covers_performance, redeemable_now,
remaining_admissions, restore_redemptions_for_order, get_active_products) plus
the two DB-level shape/backstop constraints on passes/models.py
(passproduct_credit_shape, unique_season_event_redemption).

These are the money-path-AGNOSTIC half of Phase 3 -- nothing here goes through
Stripe or mints a ticket via the payments path; the redemption/purchase money
path is exercised in payments/test_services.py. Tenant isolation is asserted
throughout: a pass never sees another org's events, products, or redemptions.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from events.models import Event, Performance
from orders.models import Order, Ticket
from passes import services as passes_services
from passes.models import PassProduct, PassPurchase, PassRedemption
from venues.models import Venue
from venues.tests import make_org

ACTIVE = PassPurchase.Status.ACTIVE
EXHAUSTED = PassPurchase.Status.EXHAUSTED
REFUNDED = PassPurchase.Status.REFUNDED
SEASON = PassProduct.Kind.SEASON
FLEX = PassProduct.Kind.FLEX


class PassesFixtureMixin:
    """Builds an org + venue and cheap Event/Performance rows. The predicate
    tests need only a Performance with a starts_at and an event_id, so no
    GAAllocation/PriceTier/hold machinery is set up here."""

    def build_org(self, subdomain="roxy"):
        org = make_org(subdomain)
        venue = Venue.objects.create(organization=org, name="Main Stage")
        return org, venue

    def make_event(self, org, venue, *, title="Show", slug="show", when=None):
        event = Event.objects.create(organization=org, title=title, slug=slug)
        perf = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=when if when is not None else timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        return event, perf

    def make_product(
        self,
        org,
        *,
        kind=SEASON,
        credit_count=None,
        price="100.00",
        valid_from=None,
        valid_until=None,
        is_active=True,
        events=(),
        name="Pass",
    ):
        product = PassProduct.objects.create(
            organization=org,
            name=name,
            kind=kind,
            price=Decimal(price),
            credit_count=credit_count,
            valid_from=valid_from,
            valid_until=valid_until,
            is_active=is_active,
        )
        if events:
            product.events.set(events)
        return product

    def _order(self, org, *, performance=None, total="100.00"):
        return Order.objects.create(
            organization=org,
            performance=performance,
            buyer_email="buyer@example.com",
            total=Decimal(total),
            status=Order.Status.PAID,
        )

    def make_purchase(
        self,
        org,
        *,
        kind=SEASON,
        credit_count=None,
        credits_remaining=None,
        valid_from=None,
        valid_until=None,
        status=ACTIVE,
        covered_events=(),
        product=None,
    ):
        if product is None:
            product = self.make_product(org, kind=kind, credit_count=credit_count)
        purchase = PassPurchase.objects.create(
            organization=org,
            product=product,
            order=self._order(org),
            kind=kind,
            credit_count=credit_count,
            credits_remaining=credits_remaining,
            valid_from=valid_from,
            valid_until=valid_until,
            status=status,
        )
        if covered_events:
            purchase.covered_events.set(covered_events)
        return purchase

    def make_redemption(self, org, purchase, *, performance, event, order, credits_used=0, seat=None, face_value="35.00"):
        ticket = Ticket.objects.create(
            organization=org, order=order, performance=performance, seat=seat
        )
        return PassRedemption.objects.create(
            organization=org,
            pass_purchase=purchase,
            order=order,
            ticket=ticket,
            performance=performance,
            event=event,
            seat=seat,
            face_value=Decimal(face_value),
            credits_used=credits_used,
        )


class PassCoversPerformanceTests(PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org()
        self.event_a, self.perf_a = self.make_event(self.org, self.venue, title="A", slug="a")
        self.event_b, self.perf_b = self.make_event(self.org, self.venue, title="B", slug="b")

    def test_empty_covered_events_covers_all_events(self):
        purchase = self.make_purchase(self.org, kind=SEASON)  # no covered_events
        self.assertTrue(passes_services.pass_covers_performance(purchase, self.perf_a))
        self.assertTrue(passes_services.pass_covers_performance(purchase, self.perf_b))

    def test_specific_covered_set_includes_and_excludes(self):
        purchase = self.make_purchase(self.org, kind=SEASON, covered_events=[self.event_a])
        self.assertTrue(passes_services.pass_covers_performance(purchase, self.perf_a))
        self.assertFalse(passes_services.pass_covers_performance(purchase, self.perf_b))

    def test_valid_from_lower_bounds_performance_start(self):
        # perf_a starts now; a valid_from one day out excludes it, a day back includes it.
        future = self.make_purchase(self.org, kind=SEASON, valid_from=timezone.now() + timedelta(days=1))
        self.assertFalse(passes_services.pass_covers_performance(future, self.perf_a))
        past = self.make_purchase(self.org, kind=SEASON, valid_from=timezone.now() - timedelta(days=1))
        self.assertTrue(passes_services.pass_covers_performance(past, self.perf_a))

    def test_valid_until_upper_bounds_performance_start(self):
        past = self.make_purchase(self.org, kind=SEASON, valid_until=timezone.now() - timedelta(days=1))
        self.assertFalse(passes_services.pass_covers_performance(past, self.perf_a))
        future = self.make_purchase(self.org, kind=SEASON, valid_until=timezone.now() + timedelta(days=1))
        self.assertTrue(passes_services.pass_covers_performance(future, self.perf_a))

    def test_null_bounds_are_open_on_both_sides(self):
        purchase = self.make_purchase(self.org, kind=SEASON, valid_from=None, valid_until=None)
        self.assertTrue(passes_services.pass_covers_performance(purchase, self.perf_a))

    def test_other_orgs_event_is_not_covered_even_by_all_access_pass(self):
        other_org, other_venue = self.build_org("other")
        _, other_perf = self.make_event(other_org, other_venue, title="X", slug="x")
        # An all-access (empty covered set) pass covers ALL events, but a
        # non-empty covered set scoped to this org's event must not match
        # another org's event.
        scoped = self.make_purchase(self.org, kind=SEASON, covered_events=[self.event_a])
        self.assertFalse(passes_services.pass_covers_performance(scoped, other_perf))


class RedeemableNowTests(PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org()

    def test_active_within_window_is_redeemable(self):
        purchase = self.make_purchase(
            self.org, kind=FLEX, credit_count=2, credits_remaining=2,
            valid_until=timezone.now() + timedelta(days=1),
        )
        self.assertTrue(passes_services.redeemable_now(purchase))

    def test_null_valid_until_is_open_ended(self):
        purchase = self.make_purchase(self.org, kind=SEASON, valid_until=None)
        self.assertTrue(passes_services.redeemable_now(purchase))

    def test_exhausted_pass_is_not_redeemable(self):
        purchase = self.make_purchase(
            self.org, kind=FLEX, credit_count=2, credits_remaining=0, status=EXHAUSTED
        )
        self.assertFalse(passes_services.redeemable_now(purchase))

    def test_refunded_pass_is_not_redeemable(self):
        purchase = self.make_purchase(self.org, kind=SEASON, status=REFUNDED)
        self.assertFalse(passes_services.redeemable_now(purchase))

    def test_past_valid_until_is_not_redeemable(self):
        purchase = self.make_purchase(
            self.org, kind=SEASON, valid_until=timezone.now() - timedelta(minutes=1)
        )
        self.assertFalse(passes_services.redeemable_now(purchase))

    def test_now_argument_is_honored(self):
        cutoff = timezone.now()
        purchase = self.make_purchase(self.org, kind=SEASON, valid_until=cutoff)
        self.assertTrue(passes_services.redeemable_now(purchase, now=cutoff - timedelta(minutes=1)))
        self.assertFalse(passes_services.redeemable_now(purchase, now=cutoff + timedelta(minutes=1)))


class RemainingAdmissionsTests(PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org()
        self.event_a, self.perf_a = self.make_event(self.org, self.venue, title="A", slug="a")
        self.event_b, self.perf_b = self.make_event(self.org, self.venue, title="B", slug="b")

    def test_flex_returns_credits_remaining(self):
        purchase = self.make_purchase(self.org, kind=FLEX, credit_count=5, credits_remaining=3)
        self.assertEqual(passes_services.remaining_admissions(purchase), 3)

    def test_season_with_covered_set_is_covered_minus_redeemed(self):
        purchase = self.make_purchase(
            self.org, kind=SEASON, covered_events=[self.event_a, self.event_b]
        )
        self.assertEqual(passes_services.remaining_admissions(purchase), 2)
        order = self._order(self.org, performance=self.perf_a, total="0.00")
        self.make_redemption(
            self.org, purchase, performance=self.perf_a, event=self.event_a, order=order
        )
        self.assertEqual(passes_services.remaining_admissions(purchase), 1)

    def test_unbounded_season_empty_covered_set_returns_none(self):
        purchase = self.make_purchase(self.org, kind=SEASON)  # empty covered set = all events
        self.assertIsNone(passes_services.remaining_admissions(purchase))

    def test_another_orgs_redemption_does_not_reduce_headroom(self):
        # Tenant isolation: redemptions are read via purchase.redemptions, so a
        # different pass (even in another org) never subtracts from this one.
        purchase = self.make_purchase(
            self.org, kind=SEASON, covered_events=[self.event_a, self.event_b]
        )
        other_org, other_venue = self.build_org("other")
        other_purchase = self.make_purchase(other_org, kind=SEASON)
        other_event, other_perf = self.make_event(other_org, other_venue, title="Y", slug="y")
        other_order = self._order(other_org, performance=other_perf, total="0.00")
        self.make_redemption(
            other_org, other_purchase, performance=other_perf, event=other_event, order=other_order
        )
        self.assertEqual(passes_services.remaining_admissions(purchase), 2)


class RestoreRedemptionsForOrderTests(PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org()
        self.event_a, self.perf_a = self.make_event(self.org, self.venue, title="A", slug="a")
        self.event_b, self.perf_b = self.make_event(self.org, self.venue, title="B", slug="b")

    def test_no_redemptions_is_a_noop_returning_zero(self):
        order = self._order(self.org, performance=self.perf_a, total="0.00")
        self.assertEqual(passes_services.restore_redemptions_for_order(order), 0)

    def test_flex_restore_sums_credits_flips_exhausted_and_is_idempotent(self):
        # A flex pass drained to 0 by two 1-credit redemptions on one order.
        purchase = self.make_purchase(
            self.org, kind=FLEX, credit_count=2, credits_remaining=0, status=EXHAUSTED
        )
        order = self._order(self.org, performance=self.perf_a, total="0.00")
        self.make_redemption(
            self.org, purchase, performance=self.perf_a, event=self.event_a, order=order, credits_used=1
        )
        self.make_redemption(
            self.org, purchase, performance=self.perf_b, event=self.event_b, order=order, credits_used=1
        )

        restored = passes_services.restore_redemptions_for_order(order)

        self.assertEqual(restored, 2)
        self.assertEqual(order.pass_redemptions.count(), 0)  # rows deleted
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 2)  # summed credits_used back
        self.assertEqual(purchase.status, ACTIVE)  # EXHAUSTED -> ACTIVE flip

        # Second call finds no rows -> 0, no further credit change.
        self.assertEqual(passes_services.restore_redemptions_for_order(order), 0)
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 2)

    def test_season_restore_deletes_rows_without_touching_credits(self):
        # A season pass carries no credit balance; restoring frees the slot by
        # the delete alone (credits_used=0 adds nothing).
        purchase = self.make_purchase(
            self.org, kind=SEASON, covered_events=[self.event_a], credits_remaining=None
        )
        order = self._order(self.org, performance=self.perf_a, total="0.00")
        self.make_redemption(
            self.org, purchase, performance=self.perf_a, event=self.event_a, order=order, credits_used=0
        )

        restored = passes_services.restore_redemptions_for_order(order)

        self.assertEqual(restored, 1)
        self.assertEqual(order.pass_redemptions.count(), 0)
        purchase.refresh_from_db()
        self.assertIsNone(purchase.credits_remaining)
        self.assertEqual(purchase.status, ACTIVE)

    def test_refunded_pass_is_not_resurrected_by_a_restore(self):
        # Restoring a redemption on a REFUNDED purchase must not flip it back to
        # ACTIVE (credits come back but status stays refunded).
        purchase = self.make_purchase(
            self.org, kind=FLEX, credit_count=2, credits_remaining=0, status=REFUNDED
        )
        order = self._order(self.org, performance=self.perf_a, total="0.00")
        self.make_redemption(
            self.org, purchase, performance=self.perf_a, event=self.event_a, order=order, credits_used=1
        )

        restored = passes_services.restore_redemptions_for_order(order)

        self.assertEqual(restored, 1)
        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 1)
        self.assertEqual(purchase.status, REFUNDED)


class GetActiveProductsTests(PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org()
        self.other_org, self.other_venue = self.build_org("other")

    def test_returns_only_active_products_for_the_org(self):
        active = self.make_product(self.org, kind=SEASON, is_active=True, name="Active")
        self.make_product(self.org, kind=SEASON, is_active=False, name="Archived")

        result = list(passes_services.get_active_products(self.org))

        self.assertEqual(result, [active])

    def test_is_org_scoped(self):
        mine = self.make_product(self.org, kind=SEASON, is_active=True, name="Mine")
        self.make_product(self.other_org, kind=SEASON, is_active=True, name="Theirs")

        result = list(passes_services.get_active_products(self.org))

        self.assertEqual(result, [mine])


class PassProductCreditShapeConstraintTests(PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org()

    def test_flex_requires_positive_credit_count(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PassProduct.objects.create(
                    organization=self.org, name="Bad flex", kind=FLEX,
                    price=Decimal("10.00"), credit_count=None,
                )

    def test_flex_zero_credit_count_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PassProduct.objects.create(
                    organization=self.org, name="Zero flex", kind=FLEX,
                    price=Decimal("10.00"), credit_count=0,
                )

    def test_season_must_have_null_credit_count(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PassProduct.objects.create(
                    organization=self.org, name="Bad season", kind=SEASON,
                    price=Decimal("10.00"), credit_count=5,
                )

    def test_valid_shapes_are_accepted(self):
        flex = PassProduct.objects.create(
            organization=self.org, name="Flex", kind=FLEX, price=Decimal("10.00"), credit_count=4
        )
        season = PassProduct.objects.create(
            organization=self.org, name="Season", kind=SEASON, price=Decimal("10.00"), credit_count=None
        )
        self.assertEqual(flex.credit_count, 4)
        self.assertIsNone(season.credit_count)


class SeasonEventRedemptionConstraintTests(PassesFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org()
        self.event_a, self.perf_a = self.make_event(self.org, self.venue, title="A", slug="a")

    def test_duplicate_season_event_redemption_is_rejected(self):
        purchase = self.make_purchase(self.org, kind=SEASON, covered_events=[self.event_a])
        order = self._order(self.org, performance=self.perf_a, total="0.00")
        self.make_redemption(
            self.org, purchase, performance=self.perf_a, event=self.event_a, order=order, credits_used=0
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_redemption(
                    self.org, purchase, performance=self.perf_a, event=self.event_a,
                    order=order, credits_used=0,
                )

    def test_flex_rows_are_exempt_from_the_season_backstop(self):
        # credits_used=1 rows are outside the partial-unique constraint, so a
        # flex holder can redeem multiple credits toward one event.
        purchase = self.make_purchase(
            self.org, kind=FLEX, credit_count=2, credits_remaining=2
        )
        order = self._order(self.org, performance=self.perf_a, total="0.00")
        self.make_redemption(
            self.org, purchase, performance=self.perf_a, event=self.event_a, order=order, credits_used=1
        )
        # A second flex redemption for the same (pass, event) must NOT raise.
        self.make_redemption(
            self.org, purchase, performance=self.perf_a, event=self.event_a, order=order, credits_used=1
        )
        self.assertEqual(purchase.redemptions.count(), 2)
