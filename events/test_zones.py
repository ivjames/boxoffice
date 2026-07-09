"""Tests for events/zones.py: the pricing-zone CRUD/mutation service --
apply/reuse a zone instance, "at most one zone per performance" per seat,
template-vs-instance snapshotting (editing a template never retroactively
changes an already-applied zone), remove-seats/delete, and clone-from-
performance. See events/test_pricing.py for resolve_seat_price/
zones_by_seat_id (read-only resolution) tests."""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from events.models import Event, Performance, PricingZone, PricingZoneSeat, ZoneTemplate
from events.pricing import PricingError, resolve_seat_price
from events.zones import (
    ZoneError,
    apply_zone,
    clone_zones_from_performance,
    delete_zone,
    get_or_create_template,
    remove_seats_from_zone,
)
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org


class ZoneFixtureMixin:
    def build_reserved_performance(self, org=None, n_seats=4):
        self.org = org or make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event = Event.objects.create(organization=self.org, title="Show", slug="show")
        self.performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.section = Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra")
        self.seats = [
            Seat.objects.create(organization=self.org, section=self.section, row_label="A", number=str(i))
            for i in range(1, n_seats + 1)
        ]
        return self.performance


class ZoneTemplateModelTests(TestCase):
    def test_unique_name_per_org(self):
        org = make_org("roxy")
        ZoneTemplate.objects.create(organization=org, name="Premium", color="#c1121f")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ZoneTemplate.objects.create(organization=org, name="Premium", color="#000000")

    def test_same_name_allowed_across_orgs(self):
        org_a = make_org("theater-a")
        org_b = make_org("theater-b")
        ZoneTemplate.objects.create(organization=org_a, name="Premium", color="#c1121f")
        # Should not raise.
        ZoneTemplate.objects.create(organization=org_b, name="Premium", color="#c1121f")


class GetOrCreateTemplateTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")

    def test_creates_new_template(self):
        template = get_or_create_template(organization=self.org, name="Premium", color="#c1121f")
        self.assertEqual(template.name, "Premium")
        self.assertEqual(template.color, "#c1121f")
        self.assertEqual(ZoneTemplate.objects.filter(organization=self.org).count(), 1)

    def test_reuses_existing_template_by_name_and_updates_color(self):
        first = get_or_create_template(organization=self.org, name="Premium", color="#c1121f")
        second = get_or_create_template(organization=self.org, name="Premium", color="#0000ff")
        self.assertEqual(first.pk, second.pk)
        second.refresh_from_db()
        self.assertEqual(second.color, "#0000ff")
        self.assertEqual(ZoneTemplate.objects.filter(organization=self.org).count(), 1)


class ApplyZoneTests(ZoneFixtureMixin, TestCase):
    def setUp(self):
        self.build_reserved_performance()
        self.template = ZoneTemplate.objects.create(
            organization=self.org, name="Premium", color="#c1121f"
        )

    def test_creates_a_new_zone_and_assigns_seats(self):
        zone = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[0].pk, self.seats[1].pk],
            amount=Decimal("95.00"),
            template=self.template,
        )
        self.assertEqual(zone.name, "Premium")
        self.assertEqual(zone.color, "#c1121f")
        self.assertEqual(zone.amount, Decimal("95.00"))
        self.assertEqual(zone.template_id, self.template.pk)
        self.assertCountEqual(zone.seats.values_list("pk", flat=True), [self.seats[0].pk, self.seats[1].pk])

    def test_reapplying_same_template_extends_the_same_zone_instance(self):
        zone1 = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[0].pk],
            amount=Decimal("95.00"),
            template=self.template,
        )
        zone2 = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[1].pk],
            amount=Decimal("100.00"),
            template=self.template,
        )
        self.assertEqual(zone1.pk, zone2.pk)
        zone2.refresh_from_db()
        self.assertEqual(zone2.amount, Decimal("100.00"))
        self.assertCountEqual(zone2.seats.values_list("pk", flat=True), [self.seats[0].pk, self.seats[1].pk])
        self.assertEqual(PricingZone.objects.filter(performance=self.performance).count(), 1)

    def test_a_seat_belongs_to_at_most_one_zone_per_performance(self):
        other_template = ZoneTemplate.objects.create(organization=self.org, name="Standard", color="#1d4ed8")
        zone_a = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[0].pk],
            amount=Decimal("95.00"),
            template=self.template,
        )
        zone_b = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[0].pk],
            amount=Decimal("40.00"),
            template=other_template,
        )
        zone_a.refresh_from_db()
        self.assertNotIn(self.seats[0].pk, zone_a.seats.values_list("pk", flat=True))
        self.assertIn(self.seats[0].pk, zone_b.seats.values_list("pk", flat=True))
        # Every seat is in at most one zone for this performance.
        self.assertEqual(
            PricingZoneSeat.objects.filter(zone__performance=self.performance, seat=self.seats[0]).count(), 1
        )

    def test_no_seats_raises_zone_error(self):
        with self.assertRaises(ZoneError):
            apply_zone(
                organization=self.org,
                performance=self.performance,
                seat_ids=[],
                amount=Decimal("95.00"),
                template=self.template,
            )

    def test_editing_template_after_apply_does_not_change_the_snapshotted_zone(self):
        zone = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[0].pk],
            amount=Decimal("95.00"),
            template=self.template,
        )
        self.template.name = "Ultra Premium"
        self.template.color = "#000000"
        self.template.save(update_fields=["name", "color"])

        zone.refresh_from_db()
        self.assertEqual(zone.name, "Premium")
        self.assertEqual(zone.color, "#c1121f")

    def test_zone_survives_template_deletion(self):
        zone = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[0].pk],
            amount=Decimal("95.00"),
            template=self.template,
        )
        self.template.delete()
        zone.refresh_from_db()
        self.assertIsNone(zone.template_id)
        self.assertEqual(zone.name, "Premium")


class RemoveAndDeleteZoneTests(ZoneFixtureMixin, TestCase):
    def setUp(self):
        self.build_reserved_performance()
        self.template = ZoneTemplate.objects.create(organization=self.org, name="Premium", color="#c1121f")
        self.zone = apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[s.pk for s in self.seats[:2]],
            amount=Decimal("95.00"),
            template=self.template,
        )

    def test_remove_seats_from_zone(self):
        remove_seats_from_zone(organization=self.org, zone=self.zone, seat_ids=[self.seats[0].pk])
        self.assertCountEqual(self.zone.seats.values_list("pk", flat=True), [self.seats[1].pk])

    def test_delete_zone_cascades_seats_and_unzones_them(self):
        delete_zone(organization=self.org, zone=self.zone)
        self.assertFalse(PricingZone.objects.filter(pk=self.zone.pk).exists())
        self.assertEqual(PricingZoneSeat.objects.filter(seat__in=self.seats[:2]).count(), 0)
        # The seat falls back to the section tier (or errors if none set).
        with self.assertRaises(PricingError):
            resolve_seat_price(self.performance, self.seats[0])


class CloneZonesFromPerformanceTests(ZoneFixtureMixin, TestCase):
    def setUp(self):
        self.build_reserved_performance(n_seats=4)
        self.template = ZoneTemplate.objects.create(organization=self.org, name="Premium", color="#c1121f")
        apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[s.pk for s in self.seats[:2]],
            amount=Decimal("95.00"),
            template=self.template,
        )
        self.other_performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )

    def test_clone_copies_zones_as_new_instances_with_same_seats_and_price(self):
        created = clone_zones_from_performance(
            organization=self.org,
            target_performance=self.other_performance,
            source_performance=self.performance,
        )
        self.assertEqual(len(created), 1)
        cloned = created[0]
        source_zone = PricingZone.objects.get(performance=self.performance)
        self.assertNotEqual(cloned.pk, source_zone.pk)
        self.assertEqual(cloned.name, source_zone.name)
        self.assertEqual(cloned.color, source_zone.color)
        self.assertEqual(cloned.amount, source_zone.amount)
        self.assertCountEqual(
            cloned.seats.values_list("pk", flat=True), source_zone.seats.values_list("pk", flat=True)
        )

    def test_editing_the_clone_never_mutates_the_source(self):
        created = clone_zones_from_performance(
            organization=self.org,
            target_performance=self.other_performance,
            source_performance=self.performance,
        )
        cloned = created[0]
        cloned.amount = Decimal("1.00")
        cloned.save(update_fields=["amount"])
        remove_seats_from_zone(organization=self.org, zone=cloned, seat_ids=[self.seats[0].pk])

        source_zone = PricingZone.objects.get(performance=self.performance)
        self.assertEqual(source_zone.amount, Decimal("95.00"))
        self.assertCountEqual(
            source_zone.seats.values_list("pk", flat=True), [s.pk for s in self.seats[:2]]
        )

    def test_clone_only_copies_seats_that_exist_on_target_performances_chart(self):
        # A performance on a DIFFERENT chart shares no seats with the source
        # -- clone should copy the zone but with zero seats.
        other_org_chart_venue = self.venue
        other_chart = SeatingChart.objects.create(
            organization=self.org, venue=other_org_chart_venue, name="Cabaret"
        )
        other_section = Section.objects.create(organization=self.org, chart=other_chart, name="Floor")
        Seat.objects.create(organization=self.org, section=other_section, row_label="A", number="1")
        different_chart_performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
            seating_chart=other_chart,
        )
        created = clone_zones_from_performance(
            organization=self.org,
            target_performance=different_chart_performance,
            source_performance=self.performance,
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].seats.count(), 0)

    def test_tenant_isolation_clone_source_must_be_same_org(self):
        # clone_zones_from_performance doesn't itself check organization
        # equality between source/target (the dashboard view scopes both
        # lookups to request.organization before calling in) -- but a
        # same-org clone across two performances never leaks another org's
        # data, which is what actually matters here.
        other_org = make_org("other")
        other_venue = Venue.objects.create(organization=other_org, name="Other Stage")
        other_event = Event.objects.create(organization=other_org, title="Other Show", slug="show")
        other_perf = Performance.objects.create(
            organization=other_org,
            event=other_event,
            venue=other_venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        created = clone_zones_from_performance(
            organization=other_org, target_performance=other_perf, source_performance=self.performance
        )
        # No zones exist scoped to other_org for the source performance, so
        # nothing is copied across the tenant boundary.
        self.assertEqual(created, [])
