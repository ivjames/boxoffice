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

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from events.models import GAAllocation, Performance, PriceTier
from orders import services
from orders.models import Hold, HoldSeat, Ticket
from orders.tests import OrdersFixtureMixin
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
            organization=self.org, hold=hold, seat=self.seat, price_tier=self.price_tier
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
