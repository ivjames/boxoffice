from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier
from venues.models import SeatingChart, Section, Venue
from venues.tests import make_org


class EventModelTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")

    def test_create_event(self):
        event = Event.objects.create(
            organization=self.org,
            title="A Midsummer Night's Dream",
            slug="a-midsummer-nights-dream",
            status=Event.Status.PUBLISHED,
        )
        self.assertEqual(str(event), "A Midsummer Night's Dream")
        self.assertEqual(event.status, "published")

    def test_slug_unique_per_org_not_globally(self):
        Event.objects.create(organization=self.org, title="Show", slug="show")
        other_org = make_org("other")

        # Same slug in a different org is fine.
        Event.objects.create(organization=other_org, title="Show", slug="show")

        # Same slug in the SAME org collides.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Event.objects.create(organization=self.org, title="Show Again", slug="show")

    def test_ga_performance_with_allocation_and_price_tier(self):
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        perf = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        allocation = GAAllocation.objects.create(organization=self.org, performance=perf, capacity=100)
        tier = PriceTier.objects.create(
            organization=self.org, performance=perf, name="GA", amount=Decimal("35.00")
        )

        self.assertEqual(perf.ga_allocation, allocation)
        self.assertEqual(tier.performance, perf)
        self.assertIsNone(tier.section)

    def test_reserved_performance_with_section_price_tier(self):
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        perf = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        tier = PriceTier.objects.create(
            organization=self.org, section=section, name="Orchestra", amount=Decimal("65.00")
        )

        self.assertEqual(tier.section, section)
        self.assertIsNone(tier.performance)
        self.assertEqual(perf.seating_mode, Performance.SeatingMode.RESERVED)

    def test_price_tier_requires_exactly_one_of_performance_or_section(self):
        # Neither set -> violates the XOR check constraint.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PriceTier.objects.create(organization=self.org, name="Orphan", amount=Decimal("10.00"))

    def test_ga_allocation_sold_cannot_exceed_capacity(self):
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        perf = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GAAllocation.objects.create(
                    organization=self.org, performance=perf, capacity=10, sold=11
                )

    def test_performance_one_ga_allocation(self):
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        perf = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org, performance=perf, capacity=50)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GAAllocation.objects.create(organization=self.org, performance=perf, capacity=50)
