"""Create a demo Organization with realistic Phase 2 data: a Venue with a
seating chart, one published Event with a GA performance and a reserved
performance, and price tiers for both. Idempotent — safe to re-run (uses
get_or_create throughout), so it doubles as manual-testing fixture data for
Phase 3+ and as a smoke test that the whole model graph wires together.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Membership
from events.models import Event, GAAllocation, Performance, PriceTier
from tenants.models import Organization
from venues.models import Seat, SeatingChart, Section, Venue

User = get_user_model()

# Fixed, well-known demo credentials -- intentionally not randomized, so
# "how do I log into the demo tenant" always has the same answer. Never used
# for a real (non-demo) Organization -- this command only ever seeds
# `--subdomain roxy`-style throwaway tenants.
DEMO_OWNER_EMAIL_TEMPLATE = "owner@{subdomain}.demo"
DEMO_OWNER_PASSWORD = "roxy-demo-owner-2026"


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
            ga_perf, ga_tier = self._create_ga_performance(org, event, venue, chart)
            reserved_perf, reserved_tiers = self._create_reserved_performance(
                org, event, venue, chart, sections
            )
            owner_email, owner_password = self._create_demo_owner(org)

        self.stdout.write(self.style.SUCCESS(f"Demo tenant ready: {org.name} ({org.subdomain})"))
        self.stdout.write(f"  Venue: {venue.name}")
        self.stdout.write(
            f"  Seating chart: {chart.name} — {sum(s.seats.count() for s in sections)} seats "
            f"across {len(sections)} sections"
        )
        self.stdout.write(f"  Event: {event.title} ({event.status})")
        self.stdout.write(f"  GA performance: {ga_perf.starts_at} — capacity {ga_perf.ga_allocation.capacity}")
        self.stdout.write(f"  Reserved performance: {reserved_perf.starts_at}")
        self.stdout.write(
            "  Pricing: Orchestra $65 default / $85 evening-premium override on the reserved "
            "performance, Balcony $45 default (see events/pricing.py)"
        )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo staff login (owner role, full access):"))
        self.stdout.write(
            f"  URL:      http://{subdomain}.localhost:8000/login/  "
            f"(or https://{subdomain}.{settings.BASE_DOMAIN}/login/ once that subdomain is provisioned)"
        )
        self.stdout.write(f"  Email:    {owner_email}")
        self.stdout.write(f"  Password: {owner_password}")
        self.stdout.write(
            "  (Use `manage.py create_staff_user` to add more staff, including "
            "manager/box_office/scanner roles.)"
        )

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

        # rows/seats_per_row/origin_* here are the docs/EDITOR.md live chart
        # editor's shape/position params (venues.generation.
        # compute_row_counts + _seat_xy) -- set to roughly match this
        # method's own hand-placed Seat grid below (rows="ABCDE"x10 /
        # "FG"x10) so opening the live editor shows a sensible starting
        # layout instead of the model's bare defaults, and gives Balcony a
        # distinct origin_y so it doesn't render on top of Orchestra.
        orchestra, _ = Section.objects.get_or_create(
            organization=org,
            chart=chart,
            name="Orchestra",
            defaults={"ordering": 0, "origin_x": 1.0, "origin_y": 0.0, "rows": 5, "seats_per_row": 10},
        )
        balcony, _ = Section.objects.get_or_create(
            organization=org,
            chart=chart,
            name="Balcony",
            defaults={"ordering": 1, "origin_x": 1.0, "origin_y": 7.0, "rows": 2, "seats_per_row": 10},
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

    def _create_ga_performance(self, org, event, venue, chart):
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
                # GA doesn't read seats off a chart, but wiring it explicitly
                # here mirrors what the Phase-A backfill migration did to
                # every pre-existing performance (see orders.services.
                # get_seating_chart) -- harmless, and keeps the demo tenant
                # representative of steady-state data.
                "seating_chart": chart,
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

    def _create_reserved_performance(self, org, event, venue, chart, sections):
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
                "seating_chart": chart,
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

        # Demonstrates the per-performance section override (events/pricing.py
        # resolve_seat_tier): Orchestra normally defaults to $65 (the
        # section-scoped tier above), but THIS performance charges a $85
        # evening premium for it. Balcony is untouched, so it still falls
        # through to its $45 default -- both behaviors are visible side by
        # side on the same performance.
        tier_orchestra_override, _ = PriceTier.objects.get_or_create(
            organization=org,
            performance=perf,
            section=orchestra,
            defaults={
                "name": "Orchestra (evening premium)",
                "amount": Decimal("85.00"),
                "currency": org.currency,
            },
        )
        tiers.append(tier_orchestra_override)
        return perf, tiers

    def _create_demo_owner(self, org):
        """One demo staff user with the 'owner' role (full dashboard +
        scanning access), so a fresh clone can log into the Phase 5
        dashboard immediately without a separate create_staff_user call.
        Idempotent: re-running the command does not reset an existing
        password (matches create_staff_user's default behavior) so a demo
        password change made by hand during testing survives a re-seed."""
        email = User.objects.normalize_email(DEMO_OWNER_EMAIL_TEMPLATE.format(subdomain=org.subdomain))
        user, created = User.objects.get_or_create(
            email=email, defaults={"first_name": "Demo", "last_name": "Owner"}
        )
        if created:
            user.set_password(DEMO_OWNER_PASSWORD)
            user.save(update_fields=["password"])
        Membership.objects.update_or_create(
            user=user, organization=org, defaults={"role": Membership.Role.OWNER}
        )
        return email, DEMO_OWNER_PASSWORD
