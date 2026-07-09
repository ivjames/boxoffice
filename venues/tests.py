from django.db import IntegrityError, transaction
from django.test import TestCase

from events.models import Event
from tenants.models import Organization
from venues.models import Seat, SeatingChart, Section, Venue


def make_org(subdomain="test", **kwargs):
    kwargs.setdefault("name", f"{subdomain.title()} Theater")
    kwargs.setdefault("slug", subdomain)
    kwargs.setdefault("contact_email", f"boxoffice@{subdomain}.example.com")
    return Organization.objects.create(subdomain=subdomain, **kwargs)


class VenueModelTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")

    def test_create_venue_seating_hierarchy(self):
        venue = Venue.objects.create(organization=self.org, name="Main Stage", address="1 Main St")
        chart = SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra", ordering=0)
        seat = Seat.objects.create(organization=self.org, section=section, row_label="A", number="1")

        self.assertEqual(venue.organization_id, self.org.id)
        self.assertEqual(str(venue), "Main Stage")
        self.assertEqual(chart.venue, venue)
        self.assertEqual(section.chart, chart)
        self.assertEqual(seat.section, section)
        self.assertEqual(str(seat), "A1")
        self.assertEqual(venue.seating_charts.count(), 1)
        self.assertEqual(self.org.venues.count(), 1)

    def test_seat_unique_per_section(self):
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        chart = SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        Seat.objects.create(organization=self.org, section=section, row_label="A", number="1")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Seat.objects.create(organization=self.org, section=section, row_label="A", number="1")

    def test_section_name_unique_per_chart(self):
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        chart = SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")
        Section.objects.create(organization=self.org, chart=chart, name="Orchestra")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Section.objects.create(organization=self.org, chart=chart, name="Orchestra")


class TenantScopingTests(TestCase):
    """Verifies the Phase 1 contract: TenantScopedManager.for_organization()
    filters by org, but the default manager does NOT auto-scope — callers
    (views, later phases) are responsible for always filtering."""

    def setUp(self):
        self.org_a = make_org("theater-a")
        self.org_b = make_org("theater-b")
        self.venue_a = Venue.objects.create(organization=self.org_a, name="Venue A")
        self.venue_b = Venue.objects.create(organization=self.org_b, name="Venue B")

    def test_for_organization_filters(self):
        scoped_a = Venue.objects.for_organization(self.org_a)
        self.assertEqual(list(scoped_a), [self.venue_a])

        scoped_b = Venue.objects.for_organization(self.org_b)
        self.assertEqual(list(scoped_b), [self.venue_b])

    def test_default_manager_does_not_auto_scope(self):
        # Documents the intentional Phase 1 behavior: Venue.objects.all()
        # returns rows from every tenant. Never rely on the manager alone
        # for isolation — always filter by request.organization.
        self.assertEqual(Venue.objects.all().count(), 2)

    def test_every_row_carries_its_organization(self):
        for venue in Venue.objects.all():
            self.assertIsNotNone(venue.organization_id)


class TenantScopedMetaIndexInheritanceTests(TestCase):
    """Phase 1 gotcha: a TenantScopedModel subclass that declares its own
    Meta MUST inherit `class Meta(TenantScopedModel.Meta)` (or repeat the
    index) or the mandatory `organization` index is silently dropped. This
    checks every Phase 2 model still carries an index covering
    `organization` after declaring its own composite indexes/constraints.
    """

    def _assert_has_organization_index(self, model):
        index_field_sets = [tuple(idx.fields) for idx in model._meta.indexes]
        has_org_index = any(fields and fields[0] == "organization" for fields in index_field_sets)
        self.assertTrue(
            has_org_index,
            f"{model.__name__}._meta.indexes has no index starting with 'organization': {index_field_sets}",
        )

    def test_venues_app_models(self):
        for model in (Venue, SeatingChart, Section, Seat):
            self._assert_has_organization_index(model)

    def test_models_are_not_accidentally_abstract(self):
        # Regression guard for the "class Meta(TenantScopedModel.Meta)"
        # footgun: if Django's abstract-reset behavior ever broke, these
        # models wouldn't have database tables at all.
        for model in (Venue, SeatingChart, Section, Seat, Event):
            self.assertFalse(model._meta.abstract)
