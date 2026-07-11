"""Seed a handful of demo tenants, each with handfuls of shows and enough
live data to give every dashboard ("admin") subsection something real to
look at:

    Overview          -- tickets sold + gross revenue + per-event / per-
                         performance revenue (needs PAID orders & tickets)
    Events            -- several shows per tenant, mixing draft & published
    Performances      -- GA and reserved-seat showings across future dates,
                         including a cancelled one
    Price tiers       -- GA flat tiers, section defaults, and a per-
                         performance evening-premium override
    Pricing zones     -- reusable ZoneTemplates applied as priced PricingZones
                         on a reserved performance's seat map
    Team              -- one Membership of every role (owner / manager /
                         box office / scanner)
    Orders            -- paid (GA + reserved), plus pending / refunded /
                         cancelled so every status shows in the list
    Venues            -- one-venue tenants plus a touring tenant with two
    Seating charts    -- one or two charts per venue, real generated Seats
    Sections          -- Orchestra / Mezzanine / Balcony with ADA seats
    (Scanning)        -- a slice of tickets marked USED (scanned at the door)
    (Guests)          -- per-theater guest accounts created at fulfillment
    (House kills)     -- PerformanceSeatBlocks pulled from sale on a seat map

This complements `create_demo_tenant` (one canonical Roxy tenant): this
command paints a whole *populated platform* for a demo/walk-through. It is
DESTRUCTIVE for the showcase subdomains only -- it deletes and recreates
those Organizations on every run (cascading all their tenant-scoped data) so
the result is a known, repeatable state. It never touches any Organization
whose subdomain isn't in SHOWCASE_SUBDOMAINS. Pass --keep to build on top of
whatever's already there instead of wiping first.

    venv/bin/python manage.py seed_showcase
    venv/bin/python manage.py seed_showcase --keep
"""

import random
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Membership
from events import zones as zone_services
from events.models import Event, GAAllocation, Performance, PriceTier
from orders import services as order_services
from orders.models import Order, OrderItem, PerformanceSeatBlock, Ticket
from payments import services as payment_services
from tenants.models import Organization
from venues import generation
from venues.models import Section, SeatingChart, Venue

User = get_user_model()

# Fixed so a run is reproducible; the command wipes+rebuilds each time.
SEED = 980

# Well-known shared password for every seeded staff login, so a demo
# walk-through has one answer to "how do I log in as a manager/scanner?".
STAFF_PASSWORD = "showcase-demo-2024"

ROLES = [
    (Membership.Role.OWNER, "Olivia", "Owner"),
    (Membership.Role.MANAGER, "Marcus", "Manager"),
    (Membership.Role.BOX_OFFICE, "Bianca", "Boxoffice"),
    (Membership.Role.SCANNER, "Sam", "Scanner"),
]

BUYERS = [
    ("ada@example.com", "Ada Lovelace"),
    ("grace@example.com", "Grace Hopper"),
    ("alan@example.com", "Alan Turing"),
    ("katherine@example.com", "Katherine Johnson"),
    ("linus@example.com", "Linus Torvalds"),
    ("margaret@example.com", "Margaret Hamilton"),
    ("dennis@example.com", "Dennis Ritchie"),
    ("radia@example.com", "Radia Perlman"),
    ("edsger@example.com", "Edsger Dijkstra"),
    ("barbara@example.com", "Barbara Liskov"),
]

# Reusable pricing-zone palette (name -> hex), applied per-performance below.
ZONE_PALETTE = [
    ("Premium", "#c1121f"),
    ("Standard", "#1d4ed8"),
    ("Value", "#15803d"),
]


# -- tenant catalogue -------------------------------------------------------
#
# Each entry drives one Organization. `charts` describes the venue(s) and
# their seat layout; `shows` describes the events + performances. Kept as
# plain data so the shape of the whole demo platform is legible in one place.

TENANTS = [
    {
        "subdomain": "roxy",
        "name": "The Roxy Theater",
        "primary": "#b8860b",
        "accent": "#1a1a2e",
        "tz": "America/New_York",
        "venues": [
            {
                "name": "The Roxy Main Stage",
                "address": "123 Broadway, New York, NY",
                "charts": [("Standard house", ["Orchestra", "Mezzanine", "Balcony"])],
            }
        ],
        "shows": [
            ("A Midsummer Night's Dream", "Theater", "published"),
            ("The Nutcracker", "Ballet", "published"),
            ("Hamilton", "Musical", "published"),
            ("An Evening of Chamber Music", "Concert", "published"),
            ("Waiting for Godot", "Theater", "draft"),
        ],
    },
    {
        "subdomain": "paramount",
        "name": "Paramount Playhouse",
        "primary": "#7b2d26",
        "accent": "#f2c14e",
        "tz": "America/Chicago",
        "venues": [
            {
                "name": "Paramount Grand Hall",
                "address": "45 State Street, Chicago, IL",
                "charts": [("Proscenium seating", ["Orchestra", "Balcony"])],
            }
        ],
        "shows": [
            ("The Phantom of the Opera", "Musical", "published"),
            ("A Christmas Carol", "Theater", "published"),
            ("Swan Lake", "Ballet", "published"),
            ("Stand-Up Showcase", "Comedy", "published"),
            ("Rent", "Musical", "draft"),
        ],
    },
    {
        "subdomain": "hillside",
        "name": "Hillside Opera House",
        "primary": "#1d4ed8",
        "accent": "#c1121f",
        "tz": "America/Los_Angeles",
        # Touring org: two venues, and the main house carries two charts
        # (standard vs. an in-the-round cabaret setup).
        "venues": [
            {
                "name": "Hillside Grand Opera House",
                "address": "800 Hill Street, Los Angeles, CA",
                "charts": [
                    ("Standard house", ["Orchestra", "Grand Tier", "Balcony"]),
                    ("Cabaret setup", ["Floor tables", "Rail"]),
                ],
            },
            {
                "name": "Hillside Amphitheater (touring)",
                "address": "12 Canyon Road, Los Angeles, CA",
                "charts": [("Lawn & terrace", ["Terrace", "Lawn"])],
            },
        ],
        "shows": [
            ("La Traviata", "Opera", "published"),
            ("Carmen", "Opera", "published"),
            ("The Magic Flute", "Opera", "published"),
            ("Jazz Under the Stars", "Concert", "published"),
            ("Rigoletto", "Opera", "draft"),
        ],
    },
    {
        "subdomain": "fringe",
        "name": "Fringe Collective",
        "primary": "#16a34a",
        "accent": "#111827",
        "tz": "America/Denver",
        # Small GA-first room -- most performances are general admission.
        "venues": [
            {
                "name": "The Black Box",
                "address": "9 Larimer Alley, Denver, CO",
                "charts": [("Flexible seating", ["Floor"])],
            }
        ],
        "shows": [
            ("Improv Jam", "Comedy", "published"),
            ("Poetry Slam Finals", "Spoken Word", "published"),
            ("Basement Tapes: A New Musical", "Musical", "published"),
            ("Solo: A One-Woman Show", "Theater", "published"),
            ("Devised Work in Progress", "Theater", "draft"),
        ],
    },
]

SHOWCASE_SUBDOMAINS = [t["subdomain"] for t in TENANTS]


class Command(BaseCommand):
    help = "Seed a handful of demo tenants with shows, orders and enough data to fill every dashboard subsection."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            action="store_true",
            help="Don't wipe the existing showcase tenants first (default is to delete + rebuild them).",
        )

    def handle(self, *args, **options):
        self.rng = random.Random(SEED)

        if not options["keep"]:
            self._wipe()

        totals = {"orgs": 0, "events": 0, "performances": 0, "orders": 0, "tickets": 0}
        for spec in TENANTS:
            counts = self._build_tenant(spec)
            totals["orgs"] += 1
            for key in ("events", "performances", "orders", "tickets"):
                totals[key] += counts[key]

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "Showcase seeded: {orgs} tenants, {events} events, {performances} "
                "performances, {orders} orders, {tickets} tickets.".format(**totals)
            )
        )
        self.stdout.write("")
        self.stdout.write("Staff logins (all share one password):")
        self.stdout.write(f"  Password: {STAFF_PASSWORD}")
        self.stdout.write("  Email:    <role>@<subdomain>.demo   e.g. owner@roxy.demo, scanner@hillside.demo")
        self.stdout.write("  Roles:    owner / manager / box_office / scanner")
        self.stdout.write("")
        self.stdout.write("Dashboards (dev):")
        for sub in SHOWCASE_SUBDOMAINS:
            self.stdout.write(f"  http://{sub}.localhost:8000/dashboard/   (or ?_tenant={sub} on localhost:8000)")

    # -- teardown ----------------------------------------------------------

    def _wipe(self):
        """Delete the showcase tenants and everything they own. A plain
        Organization.delete() cascade can't do it in one shot: Order.
        performance and Performance.venue are on_delete=PROTECT, so the
        rows have to come down child-first. Deleting Orders and Holds
        (which carry the PROTECT references to Performances/PriceTiers/Seats)
        clears those guards; the rest then cascades cleanly when the
        Organizations go."""
        orgs = list(Organization.objects.filter(subdomain__in=SHOWCASE_SUBDOMAINS))
        if not orgs:
            return
        f = {"organization__in": orgs}
        # Orders (cascade OrderItems/Tickets/Payments) and Holds (cascade
        # HoldSeats) first -- these hold every PROTECT reference to
        # Performances, PriceTiers and Seats.
        Order.objects.filter(**f).delete()
        from orders.models import Hold

        Hold.objects.filter(**f).delete()
        # Now nothing protects the Performances; deleting them cascades their
        # GAAllocations, per-performance PriceTiers/PricingZones and seat
        # blocks. Everything else comes down with the Organizations.
        Performance.objects.filter(**f).delete()
        Organization.objects.filter(pk__in=[o.pk for o in orgs]).delete()
        self.stdout.write(self.style.WARNING(f"Wiped {len(orgs)} existing showcase tenant(s)."))

    # -- one tenant --------------------------------------------------------

    def _build_tenant(self, spec):
        org = Organization.objects.create(
            name=spec["name"],
            slug=spec["subdomain"],
            subdomain=spec["subdomain"],
            primary_color=spec["primary"],
            accent_color=spec["accent"],
            timezone=spec["tz"],
            currency="USD",
            contact_email=f"boxoffice@{spec['subdomain']}.example.com",
            infra_status=Organization.InfraStatus.PROVISIONED,
        )

        self._create_team(org)

        # Build venues + charts + sections + seats. section_index maps a
        # (venue, chart) to its list of (section, default_tier) so shows can
        # price against them.
        venues = []
        for vspec in spec["venues"]:
            venue = Venue.objects.create(
                organization=org, name=vspec["name"], address=vspec["address"], timezone=spec["tz"]
            )
            charts = []
            for chart_name, section_names in vspec["charts"]:
                chart = SeatingChart.objects.create(organization=org, venue=venue, name=chart_name)
                sections = self._create_sections(org, chart, section_names)
                charts.append((chart, sections))
            venues.append((venue, charts))

        counts = {"events": 0, "performances": 0, "orders": 0, "tickets": 0}
        perf_pool = []  # (performance, kind) for later order seeding

        for idx, (title, category, status) in enumerate(spec["shows"]):
            event = Event.objects.create(
                organization=org,
                title=title,
                slug=_slug(title),
                description=f"{title} — a {category.lower()} presented by {org.name}.",
                category=category,
                status=Event.Status.DRAFT if status == "draft" else Event.Status.PUBLISHED,
            )
            counts["events"] += 1
            if status == "draft":
                continue  # draft shows intentionally carry no performances yet.

            perfs = self._create_performances(org, event, venues, show_index=idx)
            counts["performances"] += len(perfs)
            perf_pool.extend(perfs)

        # Orders / tickets / scanning / house kills across this tenant's
        # performances.
        oc, tc = self._seed_orders(org, perf_pool)
        counts["orders"] += oc
        counts["tickets"] += tc

        self.stdout.write(
            f"  {org.name} ({org.subdomain}): {counts['events']} events, "
            f"{counts['performances']} performances, {counts['orders']} orders"
        )
        return counts

    # -- team --------------------------------------------------------------

    def _create_team(self, org):
        for role, first, last in ROLES:
            email = User.objects.normalize_email(f"{role}@{org.subdomain}.demo")
            user, created = User.objects.get_or_create(
                email=email, defaults={"first_name": first, "last_name": last}
            )
            if created:
                user.set_password(STAFF_PASSWORD)
                user.save(update_fields=["password"])
            Membership.objects.update_or_create(
                user=user, organization=org, defaults={"role": role}
            )

    # -- seating -----------------------------------------------------------

    def _create_sections(self, org, chart, section_names):
        """Create the named sections on `chart` with real generated Seats.
        Returns [(section, default_price_tier)] -- each section gets a chart-
        wide default PriceTier (section set, performance null) so every
        reserved performance on this chart is priced out of the box."""
        # Rough per-section price ladder by position, front (premium) to back.
        price_ladder = [Decimal("85.00"), Decimal("65.00"), Decimal("45.00"), Decimal("35.00")]
        sections = []
        origin_y = 0.0
        for ordering, name in enumerate(section_names):
            rows, seats_per_row = self._section_shape(name)
            section = Section.objects.create(
                organization=org,
                chart=chart,
                name=name,
                ordering=ordering,
                tier=name,
                origin_x=1.0,
                origin_y=origin_y,
                rows=rows,
                seats_per_row=seats_per_row,
            )
            # A couple of ADA seats at the aisle end of the last row.
            accessible_ids = {
                (label, str(num))
                for label in [generation.generate_row_labels(rows, section.row_label_scheme)[-1]]
                for num in (1, 2)
            }
            generation.generate_seats(
                section,
                generation.compute_row_counts(
                    rows, seats_per_row, section.offset_mode, section.alt_row_seat_delta
                ),
                accessible_ids=accessible_ids,
            )
            origin_y += rows + 2  # keep sections from stacking on top of each other in the editor.

            default_tier = PriceTier.objects.create(
                organization=org,
                section=section,
                name=name,
                amount=price_ladder[min(ordering, len(price_ladder) - 1)],
                currency=org.currency,
            )
            sections.append((section, default_tier))
        return sections

    def _section_shape(self, name):
        """A believable (rows, seats_per_row) for a named section."""
        lowered = name.lower()
        if any(k in lowered for k in ("orchestra", "floor", "grand", "terrace")):
            return 8, 14
        if any(k in lowered for k in ("mezz", "tier", "rail")):
            return 4, 12
        if "lawn" in lowered:
            return 6, 16
        return 5, 12  # balcony / default

    # -- performances ------------------------------------------------------

    def _create_performances(self, org, event, venues, show_index):
        """Create 2-3 performances for a published event across future dates.
        Mixes GA and reserved seating; cancels one performance on the third
        show of each tenant so the CANCELLED status is represented."""
        primary_venue, primary_charts = venues[0]
        now = timezone.now()
        perfs = []

        # Reserved run on the primary venue's first chart.
        chart, sections = primary_charts[0]
        for n in range(2):
            starts = now + timedelta(days=7 * (show_index + 1) + n * 3, hours=19, minutes=30)
            status = Performance.Status.PUBLISHED
            # One cancelled performance somewhere in the platform.
            if show_index == 2 and n == 1:
                status = Performance.Status.CANCELLED
            perf = Performance.objects.create(
                organization=org,
                event=event,
                venue=primary_venue,
                starts_at=starts,
                seating_mode=Performance.SeatingMode.RESERVED,
                status=status,
                seating_chart=chart,
            )
            perfs.append((perf, "reserved"))

        # A GA matinee (or, for the second venue if this tenant tours, a
        # GA lawn show) so GA pricing + allocations are represented too.
        ga_venue, ga_charts = venues[-1]
        ga_chart = ga_charts[0][0]
        ga_perf = Performance.objects.create(
            organization=org,
            event=event,
            venue=ga_venue,
            starts_at=now + timedelta(days=7 * (show_index + 1) + 1, hours=14),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
            seating_chart=ga_chart,
        )
        GAAllocation.objects.create(
            organization=org, performance=ga_perf, capacity=self.rng.choice([120, 150, 200]), sold=0
        )
        PriceTier.objects.create(
            organization=org,
            performance=ga_perf,
            name="General admission",
            amount=self.rng.choice([Decimal("25.00"), Decimal("30.00"), Decimal("35.00")]),
            currency=org.currency,
        )
        perfs.append((ga_perf, "ga"))

        # On the first reserved performance, add a per-performance evening-
        # premium override on the front section + apply visual pricing zones,
        # so the Price-tiers and Pricing-zones subsections both have content.
        first_reserved = perfs[0][0]
        if first_reserved.status == Performance.Status.PUBLISHED:
            self._add_premium_override(org, first_reserved, sections)
            self._apply_zones(org, first_reserved, sections)
            self._block_house_kills(org, first_reserved, sections)

        return perfs

    def _add_premium_override(self, org, perf, sections):
        front_section, front_default = sections[0]
        PriceTier.objects.create(
            organization=org,
            performance=perf,
            section=front_section,
            name=f"{front_section.name} (evening premium)",
            amount=front_default.amount + Decimal("20.00"),
            currency=org.currency,
        )

    def _apply_zones(self, org, perf, sections):
        """Carve the front section's first two rows into a Premium zone and
        the next rows into a Standard zone, from reusable templates."""
        front_section = sections[0][0]
        seats = list(front_section.seats.order_by("row_label", "number"))
        if not seats:
            return
        row_labels = sorted({s.row_label for s in seats})
        premium_rows = set(row_labels[:2])
        standard_rows = set(row_labels[2:4])

        premium_seats = [s.id for s in seats if s.row_label in premium_rows]
        standard_seats = [s.id for s in seats if s.row_label in standard_rows]

        premium_tpl = zone_services.get_or_create_template(
            organization=org, name=ZONE_PALETTE[0][0], color=ZONE_PALETTE[0][1]
        )
        standard_tpl = zone_services.get_or_create_template(
            organization=org, name=ZONE_PALETTE[1][0], color=ZONE_PALETTE[1][1]
        )
        if premium_seats:
            zone_services.apply_zone(
                organization=org, performance=perf, seat_ids=premium_seats,
                amount=Decimal("110.00"), template=premium_tpl,
            )
        if standard_seats:
            zone_services.apply_zone(
                organization=org, performance=perf, seat_ids=standard_seats,
                amount=Decimal("75.00"), template=standard_tpl,
            )

    def _block_house_kills(self, org, perf, sections):
        """Pull a few seats from sale for this performance only (sightline /
        tech holds) so a seat map shows house kills."""
        back_section = sections[-1][0]
        seats = list(back_section.seats.order_by("row_label", "number")[:3])
        for seat in seats:
            PerformanceSeatBlock.objects.get_or_create(
                organization=org, performance=perf, seat=seat,
                defaults={"reason": self.rng.choice(["Sightline obstruction", "Tech hold", "Camera platform"])},
            )

    # -- orders / tickets / scanning --------------------------------------

    def _seed_orders(self, org, perf_pool):
        """Create a realistic spread of orders across this tenant's
        published performances: paid GA + reserved (some scanned at the
        door), plus one pending, one refunded and one cancelled so every
        Order status is visible in the list."""
        sellable = [
            (p, kind)
            for (p, kind) in perf_pool
            if p.status == Performance.Status.PUBLISHED
        ]
        if not sellable:
            return 0, 0

        orders = 0
        tickets = 0
        scanner = User.objects.get(email=f"scanner@{org.subdomain}.demo")
        buyer_i = 0

        # Paid orders across the first several performances.
        for p, kind in sellable[:6]:
            n_orders = self.rng.choice([1, 2, 2, 3])
            for _ in range(n_orders):
                buyer = BUYERS[buyer_i % len(BUYERS)]
                buyer_i += 1
                order = self._paid_order(org, p, kind, buyer)
                if order is None:
                    continue
                orders += 1
                tickets += order.tickets.count()
                # Scan roughly a third of paid orders' tickets.
                if self.rng.random() < 0.35:
                    self._scan(order, scanner)

        # One pending, one refunded, one cancelled to fill out the statuses.
        first_reserved = next((p for p, k in sellable if k == "reserved"), None)
        first_ga = next((p for p, k in sellable if k == "ga"), None)

        if first_ga is not None:
            self._pending_ga_order(org, first_ga, BUYERS[buyer_i % len(BUYERS)])
            buyer_i += 1
            orders += 1

        paid_orders = list(
            Order.objects.filter(organization=org, status=Order.Status.PAID).order_by("id")
        )
        if paid_orders:
            self._refund(paid_orders[0])
        if len(paid_orders) > 1:
            self._cancel(paid_orders[-1])

        return orders, tickets

    def _paid_order(self, org, perf, kind, buyer):
        session_key = f"seed-{uuid4().hex}"
        if kind == "ga":
            tier = perf.price_tiers.filter(section__isnull=True).first()
            if tier is None:
                return None
            qty = self.rng.choice([1, 2, 2, 4])
            try:
                hold = order_services.set_ga_hold(
                    organization=org, performance=perf, session_key=session_key,
                    user=None, price_tier=tier, quantity=qty,
                )
            except order_services.HoldError:
                return None
        else:
            priced = order_services.resolve_reserved_prices(perf)
            states = order_services.reserved_seat_states(perf)
            available = [sid for sid in priced if states.get(sid) == "available"]
            if not available:
                return None
            seat_ids = available[: self.rng.choice([1, 2, 2, 3])]
            try:
                hold = order_services.set_reserved_hold(
                    organization=org, performance=perf, session_key=session_key,
                    user=None, seat_ids=seat_ids,
                )
            except order_services.HoldError:
                return None
        if hold is None:
            return None
        return payment_services.fulfill_hold(
            hold, buyer_email=buyer[0], buyer_name=buyer[1],
            payment_ref=f"seed-{uuid4()}", provider="seed",
        )

    def _pending_ga_order(self, org, perf, buyer):
        """A genuinely un-fulfilled order (buyer bailed at checkout): status
        pending, an OrderItem, no tickets."""
        tier = perf.price_tiers.filter(section__isnull=True).first()
        if tier is None:
            return
        order = Order.objects.create(
            organization=org, performance=perf, buyer_email=buyer[0],
            buyer_name=buyer[1], total=tier.amount * 2, status=Order.Status.PENDING,
        )
        OrderItem.objects.create(
            organization=org, order=order, price_tier=tier, quantity=2, unit_amount=tier.amount
        )

    def _scan(self, order, scanner):
        now = timezone.now()
        for ticket in order.tickets.filter(status=Ticket.Status.VALID):
            ticket.status = Ticket.Status.USED
            ticket.used_at = now
            ticket.scanned_by = scanner
            ticket.save(update_fields=["status", "used_at", "scanned_by"])

    def _refund(self, order):
        order.status = Order.Status.REFUNDED
        order.save(update_fields=["status"])
        order.tickets.update(status=Ticket.Status.VOID)

    def _cancel(self, order):
        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status"])
        order.tickets.update(status=Ticket.Status.VOID)


def _slug(title):
    from django.utils.text import slugify

    return slugify(title)[:255]
