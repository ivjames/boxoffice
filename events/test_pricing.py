"""Tests for events/pricing.py: the central reserved-seat price resolver.
See its module docstring / PriceTier's docstring in events/models.py for the
override-then-default-then-error rule this exercises."""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier, PricingZone, ZoneTemplate
from events.pricing import PricingError, resolve_ga_tier, resolve_seat_price, resolve_seat_tier, zones_by_seat_id
from venues.models import Seat, SeatingChart, Section, Venue
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


class ResolveSeatPriceTests(TestCase):
    """Phase C (docs/SEATING.md "C"): resolve_seat_price's zone-then-tier
    rule -- a PricingZone containing the seat wins over resolve_seat_tier's
    existing override-then-default PriceTier rule."""

    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event = Event.objects.create(organization=self.org, title="Show", slug="show")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.orchestra = Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra")
        self.performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        self.seat_a = Seat.objects.create(
            organization=self.org, section=self.orchestra, row_label="A", number="1"
        )
        self.seat_b = Seat.objects.create(
            organization=self.org, section=self.orchestra, row_label="A", number="2"
        )

    def test_no_zone_no_tier_raises(self):
        with self.assertRaises(PricingError):
            resolve_seat_price(self.performance, self.seat_a)

    def test_falls_back_to_tier_when_unzoned(self):
        tier = PriceTier.objects.create(
            organization=self.org, section=self.orchestra, name="Orchestra", amount=Decimal("65.00")
        )
        result = resolve_seat_price(self.performance, self.seat_a)
        self.assertEqual(result.amount, Decimal("65.00"))
        self.assertEqual(result.label, "Orchestra")
        self.assertIsNone(result.color)
        self.assertFalse(result.is_zone)
        self.assertEqual(result.tier, tier)
        self.assertIsNone(result.zone)

    def test_zone_wins_over_section_default(self):
        PriceTier.objects.create(
            organization=self.org, section=self.orchestra, name="Orchestra", amount=Decimal("65.00")
        )
        zone = PricingZone.objects.create(
            organization=self.org,
            performance=self.performance,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        zone.seats.add(self.seat_a, through_defaults={"organization": self.org})

        result = resolve_seat_price(self.performance, self.seat_a)
        self.assertEqual(result.amount, Decimal("95.00"))
        self.assertEqual(result.label, "Premium")
        self.assertEqual(result.color, "#c1121f")
        self.assertTrue(result.is_zone)
        self.assertEqual(result.zone, zone)

        # An unzoned seat in the same section still falls back to the tier.
        other = resolve_seat_price(self.performance, self.seat_b)
        self.assertFalse(other.is_zone)
        self.assertEqual(other.amount, Decimal("65.00"))

    def test_zone_wins_even_with_no_tier_at_all(self):
        # A zone can price a section that has never had a PriceTier set.
        zone = PricingZone.objects.create(
            organization=self.org,
            performance=self.performance,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        zone.seats.add(self.seat_a, through_defaults={"organization": self.org})
        result = resolve_seat_price(self.performance, self.seat_a)
        self.assertEqual(result.amount, Decimal("95.00"))
        with self.assertRaises(PricingError):
            resolve_seat_price(self.performance, self.seat_b)

    def test_zone_is_scoped_to_its_own_performance(self):
        other_performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        PriceTier.objects.create(
            organization=self.org, section=self.orchestra, name="Orchestra", amount=Decimal("65.00")
        )
        zone = PricingZone.objects.create(
            organization=self.org,
            performance=self.performance,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        zone.seats.add(self.seat_a, through_defaults={"organization": self.org})

        # Same seat, different performance: the zone doesn't apply there.
        result = resolve_seat_price(other_performance, self.seat_a)
        self.assertFalse(result.is_zone)
        self.assertEqual(result.amount, Decimal("65.00"))

    def test_zones_by_seat_id_bulk_matches_single_lookup(self):
        zone = PricingZone.objects.create(
            organization=self.org,
            performance=self.performance,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        zone.seats.add(self.seat_a, through_defaults={"organization": self.org})
        bulk = zones_by_seat_id(self.performance)
        self.assertEqual(bulk, {self.seat_a.id: zone})

    def test_tenant_isolation(self):
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
        other_seat = Seat.objects.create(
            organization=other_org, section=other_section, row_label="A", number="1"
        )
        other_zone = PricingZone.objects.create(
            organization=other_org,
            performance=other_perf,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        other_zone.seats.add(other_seat, through_defaults={"organization": other_org})
        with self.assertRaises(PricingError):
            resolve_seat_price(self.performance, self.seat_a)
