"""Tests for events/pricing.py: the central reserved-seat price resolver.
See its module docstring / PriceTier's docstring in events/models.py for the
override-then-default-then-error rule this exercises."""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier
from events.pricing import PricingError, resolve_ga_tier, resolve_seat_tier
from venues.models import SeatingChart, Section, Venue
from venues.tests import make_org


class ResolveSeatTierTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event = Event.objects.create(organization=self.org, title="Show", slug="show")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.orchestra = Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra")
        self.balcony = Section.objects.create(organization=self.org, chart=self.chart, name="Balcony")
        self.matinee = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        self.evening = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )

    def test_no_pricing_at_all_raises_clear_error(self):
        with self.assertRaises(PricingError):
            resolve_seat_tier(self.matinee, self.orchestra)

    def test_section_default_used_when_no_override(self):
        default = PriceTier.objects.create(
            organization=self.org, section=self.orchestra, name="Orchestra", amount=Decimal("65.00")
        )
        self.assertEqual(resolve_seat_tier(self.matinee, self.orchestra), default)
        # Same default applies on every performance sharing the chart.
        self.assertEqual(resolve_seat_tier(self.evening, self.orchestra), default)

    def test_override_wins_over_section_default_for_its_own_performance_only(self):
        default = PriceTier.objects.create(
            organization=self.org, section=self.orchestra, name="Orchestra", amount=Decimal("65.00")
        )
        override = PriceTier.objects.create(
            organization=self.org,
            performance=self.evening,
            section=self.orchestra,
            name="Orchestra (evening premium)",
            amount=Decimal("85.00"),
        )
        self.assertEqual(resolve_seat_tier(self.evening, self.orchestra), override)
        # The matinee (no override) still falls through to the section default.
        self.assertEqual(resolve_seat_tier(self.matinee, self.orchestra), default)
        # Balcony (no override, no default here at all) is untouched by any of this.
        with self.assertRaises(PricingError):
            resolve_seat_tier(self.evening, self.balcony)

    def test_override_without_a_section_default_still_resolves(self):
        # A section can be priced ONLY via a per-performance override, with
        # no chart-wide default ever set -- resolve_seat_tier must not
        # require both to exist.
        override = PriceTier.objects.create(
            organization=self.org,
            performance=self.evening,
            section=self.balcony,
            name="Balcony (evening only)",
            amount=Decimal("50.00"),
        )
        self.assertEqual(resolve_seat_tier(self.evening, self.balcony), override)
        with self.assertRaises(PricingError):
            resolve_seat_tier(self.matinee, self.balcony)

    def test_tenant_isolation_on_tiers(self):
        other_org = make_org("other")
        other_venue = Venue.objects.create(organization=other_org, name="Other Stage")
        other_event = Event.objects.create(organization=other_org, title="Other Show", slug="show")
        other_chart = SeatingChart.objects.create(organization=other_org, venue=other_venue, name="Standard")
        other_section = Section.objects.create(organization=other_org, chart=other_chart, name="Orchestra")
        other_perf = Performance.objects.create(
            organization=other_org,
            event=other_event,
            venue=other_venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        # Same section NAME, same amount, but a completely different org --
        # must never satisfy self.org's lookups.
        PriceTier.objects.create(
            organization=other_org, section=other_section, name="Orchestra", amount=Decimal("65.00")
        )
        PriceTier.objects.create(
            organization=other_org,
            performance=other_perf,
            section=other_section,
            name="Orchestra override",
            amount=Decimal("85.00"),
        )
        with self.assertRaises(PricingError):
            resolve_seat_tier(self.matinee, self.orchestra)


class ResolveGaTierTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event = Event.objects.create(organization=self.org, title="Show", slug="show")
        self.performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org, performance=self.performance, capacity=100)

    def test_ga_tier_resolves(self):
        tier = PriceTier.objects.create(
            organization=self.org, performance=self.performance, name="GA", amount=Decimal("35.00")
        )
        self.assertEqual(resolve_ga_tier(self.performance), tier)

    def test_no_ga_tier_raises_clear_error(self):
        with self.assertRaises(PricingError):
            resolve_ga_tier(self.performance)

    def test_ga_unaffected_by_an_unrelated_section_override_on_a_different_performance(self):
        """GA pricing must be unchanged by the override feature: a section
        override that happens to share this org has zero bearing on GA
        resolution, which only ever looks at performance+section__isnull."""
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        reserved_perf = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        PriceTier.objects.create(
            organization=self.org,
            performance=reserved_perf,
            section=section,
            name="Orchestra override",
            amount=Decimal("85.00"),
        )
        ga_tier = PriceTier.objects.create(
            organization=self.org, performance=self.performance, name="GA", amount=Decimal("35.00")
        )
        self.assertEqual(resolve_ga_tier(self.performance), ga_tier)
