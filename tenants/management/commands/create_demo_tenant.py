"""Create a demo Organization with realistic Phase 2 data: a Venue with a
seating chart, one published Event with a GA performance and a reserved
performance, and price tiers for both. Idempotent — safe to re-run (uses
get_or_create throughout), so it doubles as manual-testing fixture data for
Phase 3+ and as a smoke test that the whole model graph wires together.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier
from tenants.models import Organization
from venues.models import Seat, SeatingChart, Section, Venue


class Command(BaseCommand):
    help = "Create (or refresh) a demo Organization with venue/event/pricing data for manual testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--subdomain",
            default="roxy",
            help="Subdomain slug for the demo Organization (default: roxy).",
        )

    def handle(self, *args, **options):
        subdomain = options["subdomain"]

        with transaction.atomic():
            org = self._create_organization(subdomain)
            venue = self._create_venue(org)
            chart, sections = self._create_seating(org, venue)
            event = self._create_event(org)
            ga_perf, ga_tier = self._create_ga_performance(org, event, venue)
            reserved_perf, reserved_tiers = self._create_reserved_performance(
                org, event, venue, sections
            )

        self.stdout.write(self.style.SUCCESS(f"Demo tenant ready: {org.name} ({org.subdomain})"))
        self.stdout.write(f"  Venue: {venue.name}")
        self.stdout.write(
            f"  Seating chart: {chart.name} — {sum(s.seats.count() for s in sections)} seats "
            f"across {len(sections)} sections"
        )
        self.stdout.write(f"  Event: {event.title} ({event.status})")
        self.stdout.write(f"  GA performance: {ga_perf.starts_at} — capacity {ga_perf.ga_allocation.capacity}")
        self.stdout.write(f"  Reserved performance: {reserved_perf.starts_at}")

    # -- steps ------------------------------------------------------------

    def _create_organization(self, subdomain):
        org, _ = Organization.objects.get_or_create(
            subdomain=subdomain,
            defaults={
                "name": "The Roxy Theater",
                "slug": subdomain,
                "contact_email": f"boxoffice@{subdomain}.example.com",
                "timezone": "America/New_York",
                "currency": "USD",
            },
        )
        return org

    def _create_venue(self, org):
        venue, _ = Venue.objects.get_or_create(
            organization=org,
            name="The Roxy Main Stage",
            defaults={
                "address": "123 Broadway, New York, NY",
                "timezone": "America/New_York",
            },
        )
        return venue

    def _create_seating(self, org, venue):
        chart, _ = SeatingChart.objects.get_or_create(
            organization=org, venue=venue, name="Standard house"
        )

        orchestra, _ = Section.objects.get_or_create(
            organization=org, chart=chart, name="Orchestra", defaults={"ordering": 0}
        )
        balcony, _ = Section.objects.get_or_create(
            organization=org, chart=chart, name="Balcony", defaults={"ordering": 1}
        )

        self._create_seat_grid(org, orchestra, rows="ABCDE", seats_per_row=10, y_offset=0)
        self._create_seat_grid(org, balcony, rows="FG", seats_per_row=10, y_offset=6, accessible_row="G")

        return chart, [orchestra, balcony]

    def _create_seat_grid(self, org, section, rows, seats_per_row, y_offset, accessible_row=None):
        for row_index, row_label in enumerate(rows):
            for number in range(1, seats_per_row + 1):
                Seat.objects.get_or_create(
                    organization=org,
                    section=section,
                    row_label=row_label,
                    number=str(number),
                    defaults={
                        "x": float(number),
                        "y": float(y_offset + row_index),
                        "is_accessible": row_label == accessible_row and number <= 2,
                    },
                )

    def _create_event(self, org):
        event, _ = Event.objects.get_or_create(
            organization=org,
            slug="a-midsummer-nights-dream",
            defaults={
                "title": "A Midsummer Night's Dream",
                "description": "Shakespeare's classic comedy, reimagined for the Roxy stage.",
                "category": "Theater",
                "status": Event.Status.PUBLISHED,
            },
        )
        return event

    def _create_ga_performance(self, org, event, venue):
        # `starts_at` is intentionally NOT part of the lookup: it's computed
        # relative to "now" on every run, which would break idempotency
        # (a fresh row each time) if used as a get_or_create key. Instead we
        # key on (event, venue, seating_mode) — this demo only ever creates
        # one performance of each seating mode per event.
        perf, created = Performance.objects.get_or_create(
            organization=org,
            event=event,
            venue=venue,
            seating_mode=Performance.SeatingMode.GA,
            defaults={
                "starts_at": timezone.now() + timedelta(days=14, hours=4),
                "status": Performance.Status.PUBLISHED,
            },
        )
        GAAllocation.objects.get_or_create(
            organization=org, performance=perf, defaults={"capacity": 100, "sold": 0}
        )
        tier, _ = PriceTier.objects.get_or_create(
            organization=org,
            performance=perf,
            name="General admission",
            defaults={"amount": Decimal("35.00"), "currency": org.currency},
        )
        return perf, tier

    def _create_reserved_performance(self, org, event, venue, sections):
        # Same reasoning as _create_ga_performance: keep starts_at out of the
        # lookup so re-running the command doesn't create a second row.
        perf, created = Performance.objects.get_or_create(
            organization=org,
            event=event,
            venue=venue,
            seating_mode=Performance.SeatingMode.RESERVED,
            defaults={
                "starts_at": timezone.now() + timedelta(days=21, hours=4),
                "status": Performance.Status.PUBLISHED,
            },
        )

        orchestra, balcony = sections
        tiers = []
        tier_orchestra, _ = PriceTier.objects.get_or_create(
            organization=org,
            section=orchestra,
            name="Orchestra",
            defaults={"amount": Decimal("65.00"), "currency": org.currency},
        )
        tier_balcony, _ = PriceTier.objects.get_or_create(
            organization=org,
            section=balcony,
            name="Balcony",
            defaults={"amount": Decimal("45.00"), "currency": org.currency},
        )
        tiers.extend([tier_orchestra, tier_balcony])
        return perf, tiers
