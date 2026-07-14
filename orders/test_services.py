"""Tests for orders/services.py: GA availability math, reserved-seat
availability, hold create/refresh/release, tenant isolation, and the
concurrency guarantees the whole phase is about.

Concurrency-test caveat (see individual docstrings below): Django's default
test runner gives SQLite tests an in-memory database, and each new DB
connection to ":memory:" is its own empty database — so spinning up real
background threads (each opening its own connection) would NOT exercise the
production locking path; the threads simply wouldn't see each other's data.
Rather than fight that (e.g. by forcing a file-based test DB), these tests
take the second option the brief allows: assert the service rejects a second
attempt given the first attempt's *committed* state. That's the exact
interleaving `harden_sqlite()` (transaction_mode=IMMEDIATE, so a second
writer's BEGIN blocks until the first COMMITs) and Postgres
`select_for_update()` both guarantee will happen when two real requests race
— "first one to commit wins, the second re-checks against committed truth
and is rejected." A thread/process-based test would only be meaningful
against a real multi-connection backend (Postgres, or SQLite pointed at a
shared file), which isn't what CI runs by default here. See
orders/test_concurrency_multiprocess.py for that real multi-connection test,
run against actual OS subprocesses on a shared on-disk SQLite file (and,
when POSTGRES_URL is set, against real Postgres row locking too).
"""

from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from donations.models import DonationCampaign
from events.models import GAAllocation, Performance, PriceTier, PricingZone, ZoneTemplate
from orders import services
from orders.models import Hold, HoldSeat, Order, OrderItem, Ticket
from orders.tests import OrdersFixtureMixin
from promotions.models import PromoCode
from promotions.services import PromoError
from venues.models import Seat, SeatingChart, Section
from venues.tests import make_org


class GAAvailabilityTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_ga_performance()

    def test_full_capacity_available_with_no_holds(self):
        self.assertEqual(services.ga_available(self.performance), 100)

    def test_hold_reduces_availability(self):
        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=3,
        )
        self.assertEqual(services.ga_available(self.performance), 97)

    def test_sold_count_reduces_availability(self):
        self.performance.ga_allocation.sold = 40
        self.performance.ga_allocation.save(update_fields=["sold"])
        self.assertEqual(services.ga_available(self.performance), 60)

    def test_expired_hold_not_counted(self):
        Hold.objects.create(
            organization=self.org,
            performance=self.performance,
            session_key="sess-expired",
            price_tier=self.price_tier,
            quantity=50,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(services.ga_available(self.performance), 100)

    def test_exclude_session_key_omits_own_hold(self):
        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=10,
        )
        # From another session's perspective, 10 are gone.
        self.assertEqual(services.ga_available(self.performance), 90)
        # From sess-a's own perspective (about to replace its hold), all 100
        # still look available.
        self.assertEqual(
            services.ga_available(self.performance, exclude_session_key="sess-a"), 100
        )

    def test_oversell_rejected_and_no_hold_created(self):
        with self.assertRaises(services.InsufficientAvailabilityError):
            services.set_ga_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-a",
                user=None,
                price_tier=self.price_tier,
                quantity=101,
            )
        self.assertEqual(Hold.objects.filter(performance=self.performance).count(), 0)

    def test_hold_refresh_replaces_not_stacks(self):
        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=5,
        )
        holds = Hold.objects.filter(performance=self.performance, session_key="sess-a")
        self.assertEqual(holds.count(), 1)
        self.assertEqual(holds.first().quantity, 5)
        self.assertEqual(services.ga_available(self.performance), 95)

    def test_zero_quantity_releases_hold(self):
        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=0,
        )
        self.assertEqual(Hold.objects.filter(performance=self.performance).count(), 0)
        self.assertEqual(services.ga_available(self.performance), 100)

    def test_release_hold_restores_availability(self):
        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=10,
        )
        services.release_hold(
            organization=self.org, performance=self.performance, session_key="sess-a"
        )
        self.assertEqual(services.ga_available(self.performance), 100)

    def test_sold_out_message(self):
        self.performance.ga_allocation.sold = 100
        self.performance.ga_allocation.save(update_fields=["sold"])
        with self.assertRaisesMessage(services.InsufficientAvailabilityError, "Sold out."):
            services.set_ga_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-a",
                user=None,
                price_tier=self.price_tier,
                quantity=1,
            )

    def test_concurrent_last_tickets_only_one_succeeds(self):
        """Two sessions race for the last 5 GA tickets (capacity=100,
        sold=95). Session A's hold for 5 commits first; session B's hold for
        5, re-checked afterward, must be rejected. See module docstring for
        why this simulates rather than threads the SQLite-in-memory race.
        """
        self.performance.ga_allocation.sold = 95
        self.performance.ga_allocation.save(update_fields=["sold"])

        services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=5,
        )
        with self.assertRaises(services.InsufficientAvailabilityError):
            services.set_ga_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-b",
                user=None,
                price_tier=self.price_tier,
                quantity=5,
            )
        self.assertEqual(Hold.objects.filter(performance=self.performance).count(), 1)
        self.assertEqual(
            Hold.objects.get(performance=self.performance).session_key, "sess-a"
        )


class ReservedSeatAvailabilityTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_reserved_performance()

    def test_seat_available_by_default(self):
        states = services.reserved_seat_states(self.performance)
        self.assertEqual(states[self.seat.id], "available")
        self.assertEqual(services.reserved_available_count(self.performance), 1)

    def test_seat_becomes_unavailable_once_held(self):
        services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        states = services.reserved_seat_states(self.performance, session_key="sess-b")
        self.assertEqual(states[self.seat.id], "unavailable")
        self.assertEqual(services.reserved_available_count(self.performance), 0)

    def test_seat_shows_held_by_you_for_own_session(self):
        services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        states = services.reserved_seat_states(self.performance, session_key="sess-a")
        self.assertEqual(states[self.seat.id], "held_by_you")

    def test_releasing_hold_frees_seat(self):
        services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        services.release_hold(
            organization=self.org, performance=self.performance, session_key="sess-a"
        )
        states = services.reserved_seat_states(self.performance)
        self.assertEqual(states[self.seat.id], "available")
        self.assertEqual(HoldSeat.objects.filter(seat=self.seat).count(), 0)

    def test_expired_hold_frees_seat(self):
        hold = Hold.objects.create(
            organization=self.org, performance=self.performance, session_key="sess-a"
        )
        HoldSeat.objects.create(
            organization=self.org,
            hold=hold,
            seat=self.seat,
            price_tier=self.price_tier,
            unit_amount=self.price_tier.amount,
        )
        hold.expires_at = timezone.now() - timedelta(minutes=1)
        hold.save(update_fields=["expires_at"])

        states = services.reserved_seat_states(self.performance)
        self.assertEqual(states[self.seat.id], "available")

    def test_live_ticket_blocks_hold(self):
        Ticket.objects.create(
            organization=self.org,
            order=self._make_order(),
            performance=self.performance,
            seat=self.seat,
        )
        with self.assertRaises(services.SeatUnavailableError):
            services.set_reserved_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-a",
                user=None,
                seat_ids=[self.seat.id],
            )

    def test_void_ticket_does_not_block_hold(self):
        Ticket.objects.create(
            organization=self.org,
            order=self._make_order(),
            performance=self.performance,
            seat=self.seat,
            status=Ticket.Status.VOID,
        )
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        self.assertIsNotNone(hold)

    def test_second_session_conflict_rejected_cleanly(self):
        """Session A grabs the seat first (committed). Session B's
        subsequent attempt at the same seat must fail with a clear message
        and must not create a HoldSeat. See module docstring for the
        SQLite-vs-threads caveat this simulates.
        """
        services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        with self.assertRaises(services.SeatUnavailableError) as ctx:
            services.set_reserved_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-b",
                user=None,
                seat_ids=[self.seat.id],
            )
        self.assertIn("A1", str(ctx.exception))
        self.assertEqual(HoldSeat.objects.filter(seat=self.seat).count(), 1)
        self.assertEqual(HoldSeat.objects.get(seat=self.seat).hold.session_key, "sess-a")

    def test_replacing_own_selection_does_not_self_block(self):
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        # Re-submitting the same seat for the same session should succeed
        # (not be treated as "taken by someone else").
        hold_again = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        self.assertNotEqual(hold.pk, hold_again.pk)  # replaced, not stacked
        self.assertEqual(Hold.objects.filter(session_key="sess-a").count(), 1)

    def test_partial_conflict_fails_whole_request_atomically(self):
        second_seat = Seat.objects.create(
            organization=self.org, section=self.section, row_label="A", number="2"
        )
        services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        with self.assertRaises(services.SeatUnavailableError):
            services.set_reserved_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-b",
                user=None,
                seat_ids=[self.seat.id, second_seat.id],
            )
        # sess-b must not have picked up second_seat either — all-or-nothing.
        self.assertFalse(HoldSeat.objects.filter(seat=second_seat).exists())

    def _make_order(self):
        from orders.models import Order

        return Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="buyer@example.com",
            total=Decimal("65.00"),
        )


class PerformanceSeatBlockTests(OrdersFixtureMixin, TestCase):
    """PerformanceSeatBlock ("house kill") availability wiring: a blocked
    seat reads as unavailable/unselectable exactly like a ticketed or
    held-by-someone-else seat, for this performance only."""

    def setUp(self):
        self.build_reserved_performance()

    def _block(self, performance=None, seat=None, reason=""):
        from orders.models import PerformanceSeatBlock

        return PerformanceSeatBlock.objects.create(
            organization=self.org,
            performance=performance or self.performance,
            seat=seat or self.seat,
            reason=reason,
        )

    def test_blocked_seat_has_its_own_state(self):
        self._block(reason="Sightline obstructed")
        states = services.reserved_seat_states(self.performance)
        self.assertEqual(states[self.seat.id], "blocked")

    def test_blocked_seat_excluded_from_available_count(self):
        self.assertEqual(services.reserved_available_count(self.performance), 1)
        self._block()
        self.assertEqual(services.reserved_available_count(self.performance), 0)

    def test_blocked_seat_cannot_be_held(self):
        self._block()
        with self.assertRaises(services.SeatUnavailableError):
            services.set_reserved_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-a",
                user=None,
                seat_ids=[self.seat.id],
            )
        self.assertEqual(HoldSeat.objects.filter(seat=self.seat).count(), 0)

    def test_block_is_scoped_to_one_performance(self):
        """The same seat, on a DIFFERENT performance of the same chart, is
        untouched by a block on this one."""
        other_performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        self._block()
        states = services.reserved_seat_states(other_performance)
        self.assertEqual(states[self.seat.id], "available")

    def test_unique_per_performance_and_seat(self):
        self._block()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._block()

    def test_blocked_seat_not_selectable_via_seat_map_state_helper(self):
        self._block()
        states = services.reserved_seat_states(self.performance)
        self.assertIn(states[self.seat.id], services.NOT_SELECTABLE_STATES)


class PerformanceSeatingChartFKTests(OrdersFixtureMixin, TestCase):
    """Performance.seating_chart, when set, is authoritative;
    get_seating_chart() only falls back to the venue's first chart when
    it's null -- see orders.services.get_seating_chart's docstring."""

    def setUp(self):
        self.build_reserved_performance()

    def test_falls_back_to_venues_first_chart_when_null(self):
        self.assertIsNone(self.performance.seating_chart_id)
        chart = services.get_seating_chart(self.performance)
        self.assertEqual(chart, self.section.chart)

    def test_explicit_chart_wins_over_fallback(self):
        second_chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Cabaret setup"
        )
        self.performance.seating_chart = second_chart
        self.performance.save(update_fields=["seating_chart"])

        chart = services.get_seating_chart(self.performance)
        self.assertEqual(chart, second_chart)
        # And it drives which seats/sections resolve for the performance too.
        self.assertEqual(list(services.performance_seats(self.performance)), [])

    def test_explicit_chart_used_by_reserved_seat_states(self):
        second_chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Cabaret setup"
        )
        other_section = Section.objects.create(
            organization=self.org, chart=second_chart, name="Cabaret floor"
        )
        other_seat = Seat.objects.create(
            organization=self.org, section=other_section, row_label="A", number="1"
        )
        self.performance.seating_chart = second_chart
        self.performance.save(update_fields=["seating_chart"])

        states = services.reserved_seat_states(self.performance)
        self.assertIn(other_seat.id, states)
        self.assertNotIn(self.seat.id, states)


class ReservedSeatPricingOverrideTests(OrdersFixtureMixin, TestCase):
    """price_tiers_by_section() / set_reserved_hold() must route through
    events.pricing.resolve_seat_tier, so a per-performance override on the
    section (events/pricing.py, events/models.py PriceTier docstring) is
    honored end to end -- from the seat-map price display through the
    HoldSeat.price_tier that Stripe checkout ultimately charges."""

    def setUp(self):
        self.build_reserved_performance()  # section default tier: $65.00

    def test_no_override_uses_section_default(self):
        tiers = services.price_tiers_by_section(self.performance)
        self.assertEqual(tiers[self.section.id].amount, Decimal("65.00"))

    def test_override_wins_in_price_tiers_by_section(self):
        override = PriceTier.objects.create(
            organization=self.org,
            performance=self.performance,
            section=self.section,
            name="Orchestra (evening premium)",
            amount=Decimal("85.00"),
        )
        tiers = services.price_tiers_by_section(self.performance)
        self.assertEqual(tiers[self.section.id], override)

    def test_override_does_not_leak_to_other_performances(self):
        other_perf = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        PriceTier.objects.create(
            organization=self.org,
            performance=self.performance,
            section=self.section,
            name="Orchestra (evening premium)",
            amount=Decimal("85.00"),
        )
        tiers = services.price_tiers_by_section(other_perf)
        self.assertEqual(tiers[self.section.id].amount, Decimal("65.00"))

    def test_set_reserved_hold_assigns_the_override_price_to_the_holdseat(self):
        PriceTier.objects.create(
            organization=self.org,
            performance=self.performance,
            section=self.section,
            name="Orchestra (evening premium)",
            amount=Decimal("85.00"),
        )
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        hold_seat = hold.hold_seats.get(seat=self.seat)
        self.assertEqual(hold_seat.price_tier.amount, Decimal("85.00"))
        self.assertEqual(services.hold_total(hold), Decimal("85.00"))

    def test_seat_with_no_override_and_no_default_is_omitted_and_blocks_hold(self):
        # Remove the section-default fixture tier entirely -- this seat now
        # has neither an override nor a default priced.
        self.price_tier.delete()
        tiers = services.price_tiers_by_section(self.performance)
        self.assertNotIn(self.section.id, tiers)
        with self.assertRaises(services.HoldError):
            services.set_reserved_hold(
                organization=self.org,
                performance=self.performance,
                session_key="sess-a",
                user=None,
                seat_ids=[self.seat.id],
            )


class ReservedSeatZonePricingTests(OrdersFixtureMixin, TestCase):
    """Phase C (docs/SEATING.md "C"): a PricingZone wins over the section
    PriceTier for resolve_reserved_prices/set_reserved_hold, and the
    resulting HoldSeat snapshots price_tier=None/pricing_zone/unit_amount
    correctly -- see HoldSeat's docstring for why unit_amount is the
    snapshot money-path callers must read, not a live FK."""

    def setUp(self):
        self.build_reserved_performance()  # section default tier: $65.00
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

    def test_resolve_reserved_prices_zone_wins_over_section_default(self):
        resolved = services.resolve_reserved_prices(self.performance)
        self.assertEqual(resolved[self.seat.id].amount, Decimal("95.00"))
        self.assertTrue(resolved[self.seat.id].is_zone)

    def test_set_reserved_hold_snapshots_zone_price_on_holdseat(self):
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        hold_seat = hold.hold_seats.get(seat=self.seat)
        self.assertEqual(hold_seat.unit_amount, Decimal("95.00"))
        self.assertEqual(hold_seat.pricing_zone_id, self.zone.pk)
        self.assertIsNone(hold_seat.price_tier_id)
        self.assertEqual(services.hold_total(hold), Decimal("95.00"))

    def test_unzoned_seat_on_same_performance_still_uses_section_default(self):
        second_seat = Seat.objects.create(
            organization=self.org, section=self.section, row_label="A", number="2"
        )
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id, second_seat.id],
        )
        zoned = hold.hold_seats.get(seat=self.seat)
        unzoned = hold.hold_seats.get(seat=second_seat)
        self.assertEqual(zoned.unit_amount, Decimal("95.00"))
        self.assertEqual(unzoned.unit_amount, Decimal("65.00"))
        self.assertEqual(unzoned.price_tier_id, self.price_tier.pk)
        self.assertIsNone(unzoned.pricing_zone_id)

    def test_zone_can_price_a_seat_with_no_section_tier_at_all(self):
        self.price_tier.delete()
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        hold_seat = hold.hold_seats.get(seat=self.seat)
        self.assertEqual(hold_seat.unit_amount, Decimal("95.00"))

    def test_editing_the_zone_after_hold_creation_does_not_change_the_snapshot(self):
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        self.zone.amount = Decimal("500.00")
        self.zone.save(update_fields=["amount"])

        hold_seat = hold.hold_seats.get(seat=self.seat)
        self.assertEqual(hold_seat.unit_amount, Decimal("95.00"))
        self.assertEqual(services.hold_total(hold), Decimal("95.00"))

    def test_deleting_the_zone_after_hold_creation_does_not_change_the_snapshot(self):
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        self.zone.delete()

        hold_seat = hold.hold_seats.get(seat=self.seat)
        hold_seat.refresh_from_db()
        self.assertEqual(hold_seat.unit_amount, Decimal("95.00"))
        self.assertIsNone(hold_seat.pricing_zone_id)
        self.assertEqual(services.hold_total(hold), Decimal("95.00"))


class HoldTotalTests(OrdersFixtureMixin, TestCase):
    def test_ga_hold_total(self):
        self.build_ga_performance()
        hold = services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=3,
        )
        self.assertEqual(services.hold_total(hold), Decimal("105.00"))

    def test_reserved_hold_total(self):
        self.build_reserved_performance()
        second_seat = Seat.objects.create(
            organization=self.org, section=self.section, row_label="A", number="2"
        )
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id, second_seat.id],
        )
        self.assertEqual(services.hold_total(hold), Decimal("130.00"))


class TenantIsolationTests(TestCase):
    def setUp(self):
        self.org_a = make_org("org-a")
        self.org_b = make_org("org-b")

        from venues.models import SeatingChart, Section, Venue
        from events.models import Event, PriceTier

        def build(org, subdomain_seed):
            venue = Venue.objects.create(organization=org, name=f"Venue {subdomain_seed}")
            event = Event.objects.create(organization=org, title="Show", slug="show")
            performance = Performance.objects.create(
                organization=org,
                event=event,
                venue=venue,
                starts_at=timezone.now(),
                seating_mode=Performance.SeatingMode.RESERVED,
            )
            chart = SeatingChart.objects.create(organization=org, venue=venue, name="Standard")
            section = Section.objects.create(organization=org, chart=chart, name="Orchestra")
            seat = Seat.objects.create(organization=org, section=section, row_label="A", number="1")
            tier = PriceTier.objects.create(
                organization=org, section=section, name="Orchestra", amount=Decimal("50.00")
            )
            return performance, seat, tier

        self.perf_a, self.seat_a, self.tier_a = build(self.org_a, "a")
        self.perf_b, self.seat_b, self.tier_b = build(self.org_b, "b")

    def test_hold_in_org_a_does_not_affect_org_b(self):
        services.set_reserved_hold(
            organization=self.org_a,
            performance=self.perf_a,
            session_key="shared-session-key",
            user=None,
            seat_ids=[self.seat_a.id],
        )
        # Same session key reused against org B's identical-shaped seat —
        # must be entirely unaffected by org A's hold.
        states_b = services.reserved_seat_states(self.perf_b)
        self.assertEqual(states_b[self.seat_b.id], "available")

        hold = services.set_reserved_hold(
            organization=self.org_b,
            performance=self.perf_b,
            session_key="shared-session-key",
            user=None,
            seat_ids=[self.seat_b.id],
        )
        self.assertIsNotNone(hold)
        self.assertEqual(hold.organization_id, self.org_b.id)

    def test_ga_availability_scoped_per_org(self):
        from events.models import GAAllocation

        ga_perf_a = Performance.objects.create(
            organization=self.org_a,
            event=self.perf_a.event,
            venue=self.perf_a.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org_a, performance=ga_perf_a, capacity=5)
        ga_perf_b = Performance.objects.create(
            organization=self.org_b,
            event=self.perf_b.event,
            venue=self.perf_b.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org_b, performance=ga_perf_b, capacity=5)

        from events.models import PriceTier

        tier_a = PriceTier.objects.create(
            organization=self.org_a, performance=ga_perf_a, name="GA", amount=Decimal("10.00")
        )
        services.set_ga_hold(
            organization=self.org_a,
            performance=ga_perf_a,
            session_key="sess",
            user=None,
            price_tier=tier_a,
            quantity=5,
        )
        self.assertEqual(services.ga_available(ga_perf_a), 0)
        # org B's identically-shaped GA performance is untouched.
        self.assertEqual(services.ga_available(ga_perf_b), 5)


# --- Promo-code apply / remove / net-total helpers ----------------------------
#
# apply_promo_code / remove_promo_code snapshot (or clear) the promo onto the
# Hold; hold_grand_total = max(hold_total - discount, 0); hold_currency resolves
# the single charge currency. validate_code's own rejection paths are covered in
# promotions/test_services.py -- here we prove the Hold-bound wiring.


def _make_promo(org, code="SAVE10", *, kind=PromoCode.Kind.PERCENT, value="10", **kwargs):
    return PromoCode.objects.create(
        organization=org, code=code, kind=kind, value=Decimal(value), **kwargs
    )


class ApplyPromoCodeGATests(OrdersFixtureMixin, TestCase):
    """apply/remove on a GA hold (2 x $35 tier = $70 gross)."""

    def setUp(self):
        self.build_ga_performance()
        self.hold = services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )

    def _apply(self, code):
        return services.apply_promo_code(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk, code=code
        )

    def test_percent_code_snapshots_all_three_fields_and_nets_the_total(self):
        promo = _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        hold = self._apply("save10")

        self.assertEqual(hold.discount_amount, Decimal("7.00"))  # 10% of $70
        self.assertEqual(hold.promo_code_text, "SAVE10")
        self.assertEqual(hold.promo_code_id, promo.pk)
        self.assertEqual(services.hold_total(hold), Decimal("70.00"))  # gross unchanged
        self.assertEqual(services.hold_grand_total(hold), Decimal("63.00"))  # net
        self.assertEqual(services.hold_discount(hold), Decimal("7.00"))

    def test_fixed_code_snapshots_and_nets_the_total(self):
        promo = _make_promo(self.org, code="TENOFF", kind=PromoCode.Kind.FIXED, value="10")
        hold = self._apply("tenoff")

        self.assertEqual(hold.discount_amount, Decimal("10.00"))
        self.assertEqual(hold.promo_code_text, "TENOFF")
        self.assertEqual(hold.promo_code_id, promo.pk)
        self.assertEqual(services.hold_grand_total(hold), Decimal("60.00"))

    def test_persisted_snapshot_survives_reload(self):
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        self._apply("save10")
        reloaded = Hold.objects.get(pk=self.hold.pk)
        self.assertEqual(reloaded.discount_amount, Decimal("7.00"))
        self.assertEqual(reloaded.promo_code_text, "SAVE10")

    def test_maxed_code_raises_and_leaves_hold_untouched(self):
        promo = _make_promo(self.org, value="10", max_redemptions=1)
        promo.redemption_count = 1
        promo.save(update_fields=["redemption_count"])

        with self.assertRaises(PromoError):
            self._apply("save10")

        reloaded = Hold.objects.get(pk=self.hold.pk)
        self.assertIsNone(reloaded.discount_amount)
        self.assertEqual(reloaded.promo_code_text, "")
        self.assertIsNone(reloaded.promo_code_id)

    def test_expired_code_raises_and_leaves_hold_untouched(self):
        _make_promo(self.org, value="10", ends_at=timezone.now() - timedelta(days=1))
        with self.assertRaises(PromoError):
            self._apply("save10")
        reloaded = Hold.objects.get(pk=self.hold.pk)
        self.assertIsNone(reloaded.discount_amount)

    def test_unknown_code_raises(self):
        with self.assertRaises(PromoError):
            self._apply("does-not-exist")

    def test_applying_a_second_code_replaces_the_first(self):
        _make_promo(self.org, code="SAVE10", kind=PromoCode.Kind.PERCENT, value="10")
        second = _make_promo(self.org, code="TENOFF", kind=PromoCode.Kind.FIXED, value="10")

        self._apply("save10")
        hold = self._apply("tenoff")

        self.assertEqual(hold.promo_code_text, "TENOFF")
        self.assertEqual(hold.promo_code_id, second.pk)
        self.assertEqual(hold.discount_amount, Decimal("10.00"))
        self.assertEqual(services.hold_grand_total(hold), Decimal("60.00"))

    def test_remove_clears_all_three_fields(self):
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        self._apply("save10")

        services.remove_promo_code(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk
        )

        reloaded = Hold.objects.get(pk=self.hold.pk)
        self.assertIsNone(reloaded.discount_amount)
        self.assertEqual(reloaded.promo_code_text, "")
        self.assertIsNone(reloaded.promo_code_id)
        # And the net total falls back to the gross.
        self.assertEqual(services.hold_grand_total(reloaded), Decimal("70.00"))

    def test_remove_on_missing_hold_is_a_silent_noop(self):
        # No exception even though the hold id is bogus.
        services.remove_promo_code(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk + 999
        )

    def test_garbled_hold_id_is_promo_error_not_500(self):
        # hold_id comes straight off POST data -- an empty or non-numeric value
        # must read as "no such hold" (buyer-safe PromoError / silent no-op),
        # never a ValueError out of the pk filter. Caught live in the smoke
        # drive: a crafted POST with hold_id="" 500'd before this guard.
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        for bogus in ("", "abc", None):
            with self.assertRaises(PromoError):
                services.apply_promo_code(
                    organization=self.org,
                    session_key="sess-a",
                    hold_id=bogus,
                    code="save10",
                )
            services.remove_promo_code(  # must not raise
                organization=self.org, session_key="sess-a", hold_id=bogus
            )

    def test_editing_the_promo_after_apply_does_not_move_the_snapshot(self):
        promo = _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        self._apply("save10")

        # Staff change the code AFTER it was applied and frozen onto the hold.
        promo.value = Decimal("90")
        promo.kind = PromoCode.Kind.FIXED
        promo.save(update_fields=["value", "kind"])

        reloaded = Hold.objects.get(pk=self.hold.pk)
        self.assertEqual(reloaded.discount_amount, Decimal("7.00"))  # still the frozen 10%
        self.assertEqual(services.hold_grand_total(reloaded), Decimal("63.00"))

    def test_re_running_set_ga_hold_recreates_hold_and_drops_the_promo(self):
        """set_ga_hold replaces the Hold row wholesale, so any applied promo is
        dropped -- the documented v1 behavior (re-selecting quantity clears the
        code; the buyer re-enters it)."""
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        self._apply("save10")

        new_hold = services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=3,
        )

        self.assertNotEqual(new_hold.pk, self.hold.pk)
        self.assertIsNone(new_hold.discount_amount)
        self.assertEqual(new_hold.promo_code_text, "")
        self.assertIsNone(new_hold.promo_code_id)
        self.assertFalse(Hold.objects.filter(pk=self.hold.pk).exists())

    def test_missing_hold_raises_promo_error(self):
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        with self.assertRaises(PromoError):
            services.apply_promo_code(
                organization=self.org, session_key="sess-a", hold_id=self.hold.pk + 999, code="save10"
            )

    def test_expired_hold_raises_promo_error(self):
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        self.hold.expires_at = timezone.now() - timedelta(minutes=1)
        self.hold.save(update_fields=["expires_at"])
        with self.assertRaises(PromoError):
            self._apply("save10")

    def test_another_sessions_hold_is_not_reachable(self):
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        with self.assertRaises(PromoError):
            services.apply_promo_code(
                organization=self.org, session_key="sess-other", hold_id=self.hold.pk, code="save10"
            )

    def test_min_order_amount_boundary_passes_at_exactly_min(self):
        _make_promo(self.org, value="10", min_order_amount=Decimal("70.00"))  # == $70 gross
        hold = self._apply("save10")
        self.assertEqual(hold.discount_amount, Decimal("7.00"))

    def test_min_order_amount_just_above_gross_rejected(self):
        _make_promo(self.org, value="10", min_order_amount=Decimal("70.01"))
        with self.assertRaises(PromoError):
            self._apply("save10")

    def test_hold_currency_reads_ga_tier_currency(self):
        self.assertEqual(services.hold_currency(self.hold), "USD")
        self.price_tier.currency = "EUR"
        self.price_tier.save(update_fields=["currency"])
        self.assertEqual(services.hold_currency(self.hold), "EUR")


class ApplyPromoCodeReservedTests(OrdersFixtureMixin, TestCase):
    """apply on a reserved hold (1 x $65 seat)."""

    def setUp(self):
        self.build_reserved_performance()
        self.hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )

    def _apply(self, code):
        return services.apply_promo_code(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk, code=code
        )

    def test_percent_code_on_reserved_hold(self):
        _make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="20")
        hold = self._apply("save10")
        self.assertEqual(hold.discount_amount, Decimal("13.00"))  # 20% of $65
        self.assertEqual(services.hold_grand_total(hold), Decimal("52.00"))

    def test_fixed_code_on_reserved_hold(self):
        _make_promo(self.org, code="TENOFF", kind=PromoCode.Kind.FIXED, value="15")
        hold = self._apply("tenoff")
        self.assertEqual(hold.discount_amount, Decimal("15.00"))
        self.assertEqual(services.hold_grand_total(hold), Decimal("50.00"))

    def test_hold_currency_reads_first_holdseat_tier_currency(self):
        self.assertEqual(services.hold_currency(self.hold), "USD")
        self.price_tier.currency = "GBP"
        self.price_tier.save(update_fields=["currency"])
        self.assertEqual(services.hold_currency(self.hold), "GBP")


class HoldCurrencyZoneFallbackTests(OrdersFixtureMixin, TestCase):
    """A zone-priced seat has no PriceTier (price_tier_id is None), so
    hold_currency falls back to the organization's own currency -- PricingZones
    carry no currency of their own."""

    def setUp(self):
        self.build_reserved_performance()
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
        # Remove the section tier so the seat is priced ONLY by the zone.
        self.price_tier.delete()

    def test_zone_only_hold_falls_back_to_org_currency(self):
        hold = services.set_reserved_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            seat_ids=[self.seat.id],
        )
        first = hold.hold_seats.first()
        self.assertIsNone(first.price_tier_id)  # zone-priced, no tier
        self.assertEqual(services.hold_currency(hold), self.org.currency)


class HoldGrandTotalFloorTests(OrdersFixtureMixin, TestCase):
    def test_grand_total_with_no_promo_equals_gross(self):
        self.build_ga_performance()
        hold = services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )
        self.assertEqual(services.hold_discount(hold), Decimal("0.00"))
        self.assertEqual(services.hold_grand_total(hold), services.hold_total(hold))


# --- Donation add-on (set/clear_hold_donation + grand-total math) -------------
#
# set_hold_donation freezes a gift onto the Hold exactly like apply_promo_code
# freezes a discount: org+session+unexpired-hold scoped, POST-data-tolerant
# hold_id, buyer-safe HoldError on bad input. The gift rides OUTSIDE the promo
# math (hold_grand_total = max(hold_total - discount, 0) + donation), so it's
# never discounted and never counts toward a code's min_order -- proven here on
# the Hold wiring; the Stripe/fulfillment consumption is in payments/test_services.


class SetHoldDonationGATests(OrdersFixtureMixin, TestCase):
    """set/clear_hold_donation on a GA hold (2 x $35 tier = $70 gross)."""

    def setUp(self):
        self.build_ga_performance()
        self.campaign = DonationCampaign.objects.create(organization=self.org)
        self.hold = services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )

    def _set(self, amount, campaign=None, session_key="sess-a", hold_id=None):
        return services.set_hold_donation(
            organization=self.org,
            session_key=session_key,
            hold_id=self.hold.pk if hold_id is None else hold_id,
            amount=amount,
            campaign=self.campaign if campaign is None else campaign,
        )

    def test_snapshots_amount_and_campaign_and_persists(self):
        hold = self._set("25")

        self.assertEqual(hold.donation_amount, Decimal("25.00"))
        self.assertEqual(hold.donation_campaign_id, self.campaign.pk)
        reloaded = Hold.objects.get(pk=self.hold.pk)
        self.assertEqual(reloaded.donation_amount, Decimal("25.00"))
        self.assertEqual(reloaded.donation_campaign_id, self.campaign.pk)

    def test_amount_is_quantized_to_two_places(self):
        hold = self._set("25.1")
        self.assertEqual(hold.donation_amount, Decimal("25.10"))
        hold = self._set(Decimal("9.005"))
        # Decimal quantize with default ROUND_HALF_EVEN: 9.005 -> 9.00.
        self.assertEqual(hold.donation_amount, Decimal("9.00"))

    def test_accepts_decimal_and_numeric_string_and_float(self):
        self.assertEqual(self._set(Decimal("15")).donation_amount, Decimal("15.00"))
        self.assertEqual(self._set("15").donation_amount, Decimal("15.00"))
        self.assertEqual(self._set(15.0).donation_amount, Decimal("15.00"))

    def test_grand_total_includes_the_donation_on_top_of_tickets(self):
        hold = self._set("20")
        self.assertEqual(services.hold_donation(hold), Decimal("20.00"))
        self.assertEqual(services.hold_total(hold), Decimal("70.00"))  # ticket gross unchanged
        self.assertEqual(services.hold_grand_total(hold), Decimal("90.00"))  # $70 + $20

    def test_replacing_a_donation_overwrites_the_snapshot(self):
        self._set("10")
        hold = self._set("40")
        self.assertEqual(hold.donation_amount, Decimal("40.00"))

    def test_campaign_may_be_none(self):
        hold = services.set_hold_donation(
            organization=self.org,
            session_key="sess-a",
            hold_id=self.hold.pk,
            amount="10",
            campaign=None,
        )
        self.assertEqual(hold.donation_amount, Decimal("10.00"))
        self.assertIsNone(hold.donation_campaign_id)

    def test_zero_amount_raises_holderror(self):
        with self.assertRaises(services.HoldError):
            self._set("0")
        self.assertIsNone(Hold.objects.get(pk=self.hold.pk).donation_amount)

    def test_negative_amount_raises_holderror(self):
        with self.assertRaises(services.HoldError):
            self._set("-5")

    def test_non_numeric_amount_raises_holderror(self):
        for bogus in ("abc", "", "$10"):
            with self.assertRaises(services.HoldError):
                self._set(bogus)

    def test_over_the_cap_raises_holderror(self):
        with self.assertRaises(services.HoldError):
            self._set(services.MAX_HOLD_DONATION + Decimal("0.01"))

    def test_exactly_the_cap_is_allowed(self):
        hold = self._set(services.MAX_HOLD_DONATION)
        self.assertEqual(hold.donation_amount, services.MAX_HOLD_DONATION)

    def test_missing_hold_raises_holderror(self):
        with self.assertRaises(services.HoldError):
            self._set("10", hold_id=self.hold.pk + 999)

    def test_expired_hold_raises_holderror(self):
        self.hold.expires_at = timezone.now() - timedelta(minutes=1)
        self.hold.save(update_fields=["expires_at"])
        with self.assertRaises(services.HoldError):
            self._set("10")

    def test_another_sessions_hold_is_not_reachable(self):
        with self.assertRaises(services.HoldError):
            self._set("10", session_key="sess-other")
        self.assertIsNone(Hold.objects.get(pk=self.hold.pk).donation_amount)

    def test_another_orgs_hold_is_not_reachable(self):
        other_org = make_org("org-other")
        with self.assertRaises(services.HoldError):
            services.set_hold_donation(
                organization=other_org,
                session_key="sess-a",
                hold_id=self.hold.pk,
                amount="10",
                campaign=None,
            )
        self.assertIsNone(Hold.objects.get(pk=self.hold.pk).donation_amount)

    def test_garbled_hold_id_is_holderror_not_500(self):
        # hold_id comes straight off POST data -- empty / non-numeric / None must
        # read as "no such hold" (buyer-safe HoldError), never a ValueError 500
        # out of the pk filter (mirrors apply_promo_code's guard).
        for bogus in ("", "abc", None):
            with self.assertRaises(services.HoldError):
                services.set_hold_donation(
                    organization=self.org,
                    session_key="sess-a",
                    hold_id=bogus,
                    amount="10",
                    campaign=self.campaign,
                )

    def test_clear_removes_both_snapshot_fields(self):
        self._set("30")
        services.clear_hold_donation(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk
        )
        reloaded = Hold.objects.get(pk=self.hold.pk)
        self.assertIsNone(reloaded.donation_amount)
        self.assertIsNone(reloaded.donation_campaign_id)
        # And the grand total falls back to the ticket gross.
        self.assertEqual(services.hold_grand_total(reloaded), Decimal("70.00"))

    def test_clear_on_missing_hold_is_a_silent_noop(self):
        # No exception even though the hold id is bogus.
        services.clear_hold_donation(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk + 999
        )

    def test_clear_with_garbled_hold_id_is_a_silent_noop(self):
        for bogus in ("", "abc", None):
            services.clear_hold_donation(
                organization=self.org, session_key="sess-a", hold_id=bogus
            )


class DonationDoesNotInteractWithPromoTests(OrdersFixtureMixin, TestCase):
    """The donation rides OUTSIDE the ticket-money math (hold_grand_total):
    a percentage code discounts only the tickets (never the gift), and the gift
    never counts toward a code's min_order threshold -- both fall out of keeping
    the donation out of hold_total, not a special-case branch."""

    def setUp(self):
        self.build_ga_performance()  # 2 x $35 = $70 ticket gross
        self.campaign = DonationCampaign.objects.create(organization=self.org)
        self.hold = services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=2,
        )

    def _add_donation(self, amount="40"):
        return services.set_hold_donation(
            organization=self.org,
            session_key="sess-a",
            hold_id=self.hold.pk,
            amount=amount,
            campaign=self.campaign,
        )

    def _apply(self, code, **kwargs):
        _make_promo(self.org, code=code, **kwargs)
        return services.apply_promo_code(
            organization=self.org, session_key="sess-a", hold_id=self.hold.pk, code=code
        )

    def test_percent_code_discounts_only_the_tickets_not_the_gift(self):
        self._add_donation("40")
        hold = self._apply("SAVE10", kind=PromoCode.Kind.PERCENT, value="10")
        # 10% of the $70 TICKET gross = $7 (NOT 10% of $110).
        self.assertEqual(hold.discount_amount, Decimal("7.00"))
        # Net = (70 - 7) + 40 = 103.
        self.assertEqual(services.hold_grand_total(hold), Decimal("103.00"))

    def test_donation_added_after_the_code_does_not_move_the_discount(self):
        self._apply("SAVE10", kind=PromoCode.Kind.PERCENT, value="10")  # $7 off $70
        hold = self._add_donation("40")
        self.assertEqual(services.hold_discount(hold), Decimal("7.00"))
        self.assertEqual(services.hold_grand_total(hold), Decimal("103.00"))

    def test_gift_does_not_count_toward_min_order_threshold(self):
        # A $40 gift can't inflate the $70 ticket cart past a code that needs a
        # $100 minimum -- validate_code reads hold_total (ticket gross) only.
        self._add_donation("40")
        with self.assertRaises(PromoError):
            self._apply("BIG", kind=PromoCode.Kind.FIXED, value="5", min_order_amount=Decimal("100.00"))

    def test_min_order_met_by_tickets_alone_still_applies_with_a_gift_present(self):
        self._add_donation("40")
        hold = self._apply(
            "OK", kind=PromoCode.Kind.FIXED, value="5", min_order_amount=Decimal("70.00")
        )
        self.assertEqual(hold.discount_amount, Decimal("5.00"))
        self.assertEqual(services.hold_grand_total(hold), Decimal("105.00"))  # (70-5)+40


class VoidOrderNullPerformanceTests(OrdersFixtureMixin, TestCase):
    """void_order must tolerate a null-performance order (a donation-only order
    reserves no performance and mints no ticket): no crash, nothing voided, and
    no GAAllocation decrement -- the FK read is guarded (see void_order's GA
    branch)."""

    def setUp(self):
        self.build_ga_performance()
        self.campaign = DonationCampaign.objects.create(organization=self.org)

    def test_void_on_donation_only_order_returns_zero_and_touches_no_inventory(self):
        order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="donor@example.com",
            total=Decimal("50.00"),
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal("50.00"),
            donation_campaign=self.campaign,
        )
        # Independent GA performance's allocation must be left alone.
        self.performance.ga_allocation.sold = 10
        self.performance.ga_allocation.save(update_fields=["sold"])

        voided = services.void_order(order)

        self.assertEqual(voided, 0)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 10)  # untouched


class OrderItemKindDefaultTests(OrdersFixtureMixin, TestCase):
    """OrderItem.kind defaults to "ticket" both in Python (model default) and
    at the DB level (db_default), so the column backfills every pre-Phase-2 row
    to "ticket" without a data migration -- same pattern as
    Organization.infra_status."""

    def setUp(self):
        self.build_ga_performance()
        self.order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="a@example.com",
            total=Decimal("35.00"),
        )

    def test_model_level_default_is_ticket(self):
        item = OrderItem.objects.create(
            organization=self.org,
            order=self.order,
            price_tier=self.price_tier,
            quantity=1,
            unit_amount=Decimal("35.00"),
        )
        self.assertEqual(item.kind, OrderItem.Kind.TICKET)
        self.assertEqual(item.kind, "ticket")

    def test_db_default_backfills_a_raw_insert_that_omits_the_column(self):
        """A row inserted WITHOUT the kind column (the existing-row / raw-insert
        semantics the db_default backfills) reads back as "ticket" -- proving
        the default lives in the schema, not just Django's Python layer."""
        table = OrderItem._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table} "
                "(organization_id, order_id, quantity, unit_amount) "
                "VALUES (%s, %s, %s, %s)",
                [self.org.pk, self.order.pk, 1, "35.00"],
            )
        item = OrderItem.objects.get(order=self.order)
        self.assertEqual(item.kind, "ticket")
