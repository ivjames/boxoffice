from django.core.management import CommandError, call_command
from django.test import TestCase

from events.models import Event, GAAllocation, Performance, PriceTier
from tenants.models import Organization
from venues.models import Seat, SeatingChart, Section, Venue


class CreateDemoTenantCommandTests(TestCase):
    def _counts(self):
        return {
            "orgs": Organization.objects.count(),
            "venues": Venue.objects.count(),
            "charts": SeatingChart.objects.count(),
            "sections": Section.objects.count(),
            "seats": Seat.objects.count(),
            "events": Event.objects.count(),
            "performances": Performance.objects.count(),
            "price_tiers": PriceTier.objects.count(),
            "ga_allocations": GAAllocation.objects.count(),
        }

    def test_creates_expected_rows(self):
        call_command("create_demo_tenant")

        org = Organization.objects.get(subdomain="roxy")
        self.assertTrue(org.is_active)

        venue = Venue.objects.get(organization=org)
        chart = SeatingChart.objects.get(organization=org, venue=venue)
        self.assertEqual(Section.objects.filter(organization=org, chart=chart).count(), 2)
        self.assertEqual(Seat.objects.filter(organization=org).count(), 70)

        event = Event.objects.get(organization=org)
        self.assertEqual(event.status, Event.Status.PUBLISHED)

        performances = Performance.objects.filter(organization=org, event=event)
        self.assertEqual(performances.count(), 2)

        ga_perf = performances.get(seating_mode=Performance.SeatingMode.GA)
        self.assertTrue(GAAllocation.objects.filter(performance=ga_perf, capacity=100).exists())

        reserved_perf = performances.get(seating_mode=Performance.SeatingMode.RESERVED)
        # 3 section-scoped tiers: Orchestra default, Balcony default, and the
        # Orchestra evening-premium override on the reserved performance
        # (events/pricing.py) -- see test_reserved_performance_pricing_has_a_demo_override.
        self.assertEqual(
            PriceTier.objects.filter(organization=org, section__chart=chart).count(), 3
        )
        self.assertTrue(PriceTier.objects.filter(organization=org, performance=ga_perf).exists())
        self.assertIsNotNone(reserved_perf)

    def test_reserved_performance_pricing_has_a_demo_override(self):
        """create_demo_tenant seeds ONE example per-performance section
        override (events/pricing.py resolve_seat_tier) so the feature is
        demonstrable out of the box: Orchestra's evening premium on the
        reserved performance beats its section-wide default."""
        call_command("create_demo_tenant")
        org = Organization.objects.get(subdomain="roxy")
        venue = Venue.objects.get(organization=org)
        chart = SeatingChart.objects.get(organization=org, venue=venue)
        orchestra = Section.objects.get(organization=org, chart=chart, name="Orchestra")
        balcony = Section.objects.get(organization=org, chart=chart, name="Balcony")
        reserved_perf = Performance.objects.get(
            organization=org, seating_mode=Performance.SeatingMode.RESERVED
        )

        default_tier = PriceTier.objects.get(
            organization=org, section=orchestra, performance__isnull=True
        )
        self.assertEqual(default_tier.amount, 65)

        override_tier = PriceTier.objects.get(
            organization=org, section=orchestra, performance=reserved_perf
        )
        self.assertEqual(override_tier.amount, 85)

        from events.pricing import resolve_seat_tier

        self.assertEqual(resolve_seat_tier(reserved_perf, orchestra), override_tier)
        # Balcony has no override -- still resolves to its plain default.
        balcony_tier = PriceTier.objects.get(organization=org, section=balcony)
        self.assertEqual(resolve_seat_tier(reserved_perf, balcony), balcony_tier)

    def test_idempotent_on_rerun(self):
        call_command("create_demo_tenant")
        first = self._counts()

        call_command("create_demo_tenant")
        second = self._counts()

        self.assertEqual(first, second)

    def test_custom_subdomain(self):
        call_command("create_demo_tenant", "--subdomain=globe")
        self.assertTrue(Organization.objects.filter(subdomain="globe").exists())


class ProvisionTenantCommandTests(TestCase):
    """Covers the DB half of `bin/boxoffice add-tenant` (no-wildcard onboarding)."""

    def test_creates_organization(self):
        call_command("provision_tenant", "roxy", "--name", "The Roxy Theater")

        org = Organization.objects.get(subdomain="roxy")
        self.assertEqual(org.name, "The Roxy Theater")
        self.assertEqual(org.slug, "roxy")
        self.assertTrue(org.is_active)
        self.assertEqual(org.contact_email, "boxoffice@roxy.localhost")

    def test_default_name_derived_from_subdomain(self):
        call_command("provision_tenant", "the-globe")
        org = Organization.objects.get(subdomain="the-globe")
        self.assertEqual(org.name, "The Globe")

    def test_idempotent_on_rerun(self):
        call_command("provision_tenant", "roxy", "--name", "The Roxy Theater")
        first_count = Organization.objects.count()

        # Re-running with a different --name does not rename the existing org.
        call_command("provision_tenant", "roxy", "--name", "Something Else")

        self.assertEqual(Organization.objects.count(), first_count)
        self.assertEqual(Organization.objects.get(subdomain="roxy").name, "The Roxy Theater")

    def test_rejects_reserved_subdomain(self):
        with self.assertRaises(CommandError):
            call_command("provision_tenant", "www")
        self.assertFalse(Organization.objects.filter(subdomain="www").exists())

    def test_rejects_invalid_subdomain(self):
        with self.assertRaises(CommandError):
            call_command("provision_tenant", "Not_Valid!")

    def test_custom_contact_email(self):
        call_command("provision_tenant", "roxy", "--contact-email", "box@roxy.example")
        self.assertEqual(
            Organization.objects.get(subdomain="roxy").contact_email, "box@roxy.example"
        )


class RemoveTenantCommandTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="The Roxy Theater",
            slug="roxy",
            subdomain="roxy",
            contact_email="box@roxy.example",
        )

    def test_default_deactivates_without_deleting(self):
        call_command("remove_tenant", "roxy")

        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)
        self.assertTrue(Organization.objects.filter(subdomain="roxy").exists())

    def test_deactivate_is_idempotent(self):
        call_command("remove_tenant", "roxy")
        call_command("remove_tenant", "roxy")

        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)

    def test_purge_deletes_the_organization(self):
        call_command("remove_tenant", "roxy", "--purge")
        self.assertFalse(Organization.objects.filter(subdomain="roxy").exists())

    def test_unknown_subdomain_raises(self):
        with self.assertRaises(CommandError):
            call_command("remove_tenant", "does-not-exist")
