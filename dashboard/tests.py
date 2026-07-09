"""Dashboard HTTP-layer tests: role gating for each area (any-staff /
manager+ / box-office+), tenant isolation on every list/detail view, CRUD
correctness (including that organization can never be spoofed via POST
data), and the overview report numbers."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from events.models import Event, GAAllocation, Performance, PriceTier
from orders.models import Order, Ticket
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org


class DashFixtureMixin:
    def build_org(self, subdomain):
        org = make_org(subdomain)
        venue = Venue.objects.create(organization=org, name="Main Stage")
        return org, venue

    def build_ga_event(self, org, venue, slug="ga-show", capacity=10, sold=0):
        event = Event.objects.create(
            organization=org, title="GA Show", slug=slug, status=Event.Status.PUBLISHED
        )
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now() + timedelta(days=3),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
        )
        GAAllocation.objects.create(organization=org, performance=performance, capacity=capacity, sold=sold)
        tier = PriceTier.objects.create(
            organization=org, performance=performance, name="GA", amount=Decimal("20.00")
        )
        return event, performance, tier

    def build_reserved_event(self, org, venue, slug="reserved-show", n_seats=3):
        event = Event.objects.create(
            organization=org, title="Reserved Show", slug=slug, status=Event.Status.PUBLISHED
        )
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now() + timedelta(days=3),
            seating_mode=Performance.SeatingMode.RESERVED,
            status=Performance.Status.PUBLISHED,
        )
        chart = SeatingChart.objects.create(organization=org, venue=venue, name="Standard")
        section = Section.objects.create(organization=org, chart=chart, name="Orchestra")
        seats = [
            Seat.objects.create(organization=org, section=section, row_label="A", number=str(i))
            for i in range(1, n_seats + 1)
        ]
        return event, performance, section, seats

    def make_paid_order(self, org, performance, total, n_tickets=1, email="buyer@example.com"):
        order = Order.objects.create(
            organization=org,
            performance=performance,
            buyer_email=email,
            total=Decimal(total),
            status=Order.Status.PAID,
        )
        for _ in range(n_tickets):
            Ticket.objects.create(organization=org, order=order, performance=performance)
        return order


class RoleGateTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """Each area is gated to the role level documented on Membership: any
    staff for the overview, manager+ for event/performance CRUD, box_office+
    for orders. can_scan() is exercised in scanning/tests.py."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.event, self.performance, _tier = self.build_ga_event(self.org, self.venue)
        self.roles = {
            "owner": self.make_staff(self.org, Membership.Role.OWNER)[0],
            "manager": self.make_staff(self.org, Membership.Role.MANAGER)[0],
            "box_office": self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0],
            "scanner": self.make_staff(self.org, Membership.Role.SCANNER)[0],
        }

    def _login_as(self, role):
        user = self.roles[role]
        self.client.force_login(user)

    def test_overview_open_to_every_role(self):
        for role in ["owner", "manager", "box_office", "scanner"]:
            self.client.logout()
            self._login_as(role)
            resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, 200, f"{role} should reach the overview")

    def test_event_list_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self.client.logout()
            self._login_as(role)
            resp = self.client.get("/dashboard/events/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> event list")

    def test_event_create_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self.client.logout()
            self._login_as(role)
            resp = self.client.get("/dashboard/events/new/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> event create")

    def test_order_list_box_office_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 200), ("scanner", 403)]:
            self.client.logout()
            self._login_as(role)
            resp = self.client.get("/dashboard/orders/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> order list")

    def test_scanner_cannot_reach_event_crud_or_orders(self):
        self._login_as("scanner")
        self.assertEqual(self.client.get("/dashboard/events/", HTTP_HOST=host_for("roxy")).status_code, 403)
        self.assertEqual(
            self.client.get("/dashboard/events/new/", HTTP_HOST=host_for("roxy")).status_code, 403
        )
        self.assertEqual(self.client.get("/dashboard/orders/", HTTP_HOST=host_for("roxy")).status_code, 403)

    def test_box_office_cannot_reach_event_crud(self):
        self._login_as("box_office")
        self.assertEqual(self.client.get("/dashboard/events/", HTTP_HOST=host_for("roxy")).status_code, 403)
        self.assertEqual(
            self.client.get("/dashboard/events/new/", HTTP_HOST=host_for("roxy")).status_code, 403
        )

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_platform_host_404s(self):
        self._login_as("owner")
        resp = self.client.get("/dashboard/")  # no tenant host
        self.assertEqual(resp.status_code, 404)


class TenantIsolationTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org_a, self.venue_a = self.build_org("org-a")
        self.org_b, self.venue_b = self.build_org("org-b")
        self.event_a, self.perf_a, _ = self.build_ga_event(self.org_a, self.venue_a, slug="show-a")
        self.event_b, self.perf_b, _ = self.build_ga_event(self.org_b, self.venue_b, slug="show-b")
        self.owner_a, self.password_a = self.make_staff(self.org_a, Membership.Role.OWNER)

    def test_event_list_shows_only_this_orgs_events(self):
        self.client.force_login(self.owner_a)
        resp = self.client.get("/dashboard/events/", HTTP_HOST=host_for("org-a"))
        self.assertContains(resp, "GA Show")
        content = resp.content.decode()
        self.assertNotIn(f"/dashboard/events/{self.event_b.pk}/", content)

    def test_event_detail_cross_org_404s(self):
        self.client.force_login(self.owner_a)
        resp = self.client.get(f"/dashboard/events/{self.event_b.pk}/", HTTP_HOST=host_for("org-a"))
        self.assertEqual(resp.status_code, 404)

    def test_performance_edit_cross_org_404s(self):
        self.client.force_login(self.owner_a)
        resp = self.client.get(f"/dashboard/performances/{self.perf_b.pk}/edit/", HTTP_HOST=host_for("org-a"))
        self.assertEqual(resp.status_code, 404)

    def test_order_detail_cross_org_404s(self):
        order_b = self.make_paid_order(self.org_b, self.perf_b, "20.00")
        # Give owner_a box-office access too (still org-a-scoped).
        Membership.objects.filter(user=self.owner_a, organization=self.org_a).update(
            role=Membership.Role.BOX_OFFICE
        )
        self.client.force_login(self.owner_a)
        resp = self.client.get(f"/dashboard/orders/{order_b.token}/", HTTP_HOST=host_for("org-a"))
        self.assertEqual(resp.status_code, 404)

    def test_order_list_never_leaks_other_orgs_orders(self):
        self.make_paid_order(self.org_a, self.perf_a, "20.00", email="a@example.com")
        self.make_paid_order(self.org_b, self.perf_b, "20.00", email="b@example.com")
        self.client.force_login(self.owner_a)
        resp = self.client.get("/dashboard/orders/", HTTP_HOST=host_for("org-a"))
        self.assertContains(resp, "a@example.com")
        self.assertNotContains(resp, "b@example.com")


class EventPerformanceCRUDTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager, self.password = self.make_staff(self.org, Membership.Role.MANAGER)
        self.client.force_login(self.manager)

    def test_create_event_is_scoped_to_current_org_even_if_spoofed(self):
        resp = self.client.post(
            "/dashboard/events/new/",
            {
                "title": "New Show",
                "slug": "new-show",
                "description": "",
                "category": "Theater",
                "status": Event.Status.DRAFT,
                "organization": self.other_org.pk,  # not a real form field -> ignored
            },
            HTTP_HOST=host_for("roxy"),
        )
        event = Event.objects.get(slug="new-show")
        self.assertEqual(event.organization_id, self.org.id)
        self.assertRedirects(resp, f"/dashboard/events/{event.pk}/", fetch_redirect_response=False)

    def test_edit_event_publishes_it(self):
        event = Event.objects.create(
            organization=self.org, title="Draft Show", slug="draft-show", status=Event.Status.DRAFT
        )
        resp = self.client.post(
            f"/dashboard/events/{event.pk}/edit/",
            {
                "title": "Draft Show",
                "slug": "draft-show",
                "description": "",
                "category": "",
                "status": Event.Status.PUBLISHED,
            },
            HTTP_HOST=host_for("roxy"),
        )
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.PUBLISHED)

    def test_create_ga_performance_creates_allocation(self):
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        resp = self.client.post(
            f"/dashboard/events/{event.pk}/performances/new/",
            {
                "venue": self.venue.pk,
                "starts_at": "2030-01-01T19:00",
                "seating_mode": Performance.SeatingMode.GA,
                "status": Performance.Status.DRAFT,
                "ga_capacity": "150",
            },
            HTTP_HOST=host_for("roxy"),
        )
        performance = Performance.objects.get(event=event)
        self.assertRedirects(
            resp, f"/dashboard/events/{event.pk}/", fetch_redirect_response=False
        )
        self.assertEqual(performance.ga_allocation.capacity, 150)
        self.assertEqual(performance.organization_id, self.org.id)

    def test_ga_performance_requires_capacity(self):
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        resp = self.client.post(
            f"/dashboard/events/{event.pk}/performances/new/",
            {
                "venue": self.venue.pk,
                "starts_at": "2030-01-01T19:00",
                "seating_mode": Performance.SeatingMode.GA,
                "status": Performance.Status.DRAFT,
                "ga_capacity": "",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)  # re-renders form with error
        self.assertFalse(Performance.objects.filter(event=event).exists())

    def test_reducing_ga_capacity_below_sold_rejected(self):
        _event, performance, _tier = self.build_ga_event(self.org, self.venue, capacity=10, sold=8)
        resp = self.client.post(
            f"/dashboard/performances/{performance.pk}/edit/",
            {
                "venue": self.venue.pk,
                "starts_at": performance.starts_at.strftime("%Y-%m-%dT%H:%M"),
                "seating_mode": Performance.SeatingMode.GA,
                "status": Performance.Status.PUBLISHED,
                "ga_capacity": "5",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already sold")
        performance.ga_allocation.refresh_from_db()
        self.assertEqual(performance.ga_allocation.capacity, 10)

    def test_venue_field_scoped_to_org_only(self):
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        resp = self.client.get(f"/dashboard/events/{event.pk}/performances/new/", HTTP_HOST=host_for("roxy"))
        form = resp.context["form"]
        self.assertNotIn(self.other_venue, list(form.fields["venue"].queryset))

    def test_add_ga_price_tier(self):
        _event, performance, _tier = self.build_ga_event(self.org, self.venue)
        resp = self.client.post(
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            {"name": "Premium", "amount": "50.00", "currency": "USD"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp,
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            fetch_redirect_response=False,
        )
        tier = PriceTier.objects.get(name="Premium")
        self.assertEqual(tier.performance_id, performance.pk)
        self.assertIsNone(tier.section_id)
        self.assertEqual(tier.organization_id, self.org.id)

    def test_add_reserved_price_tier_requires_section(self):
        _event, performance, section, _seats = self.build_reserved_event(self.org, self.venue)
        resp = self.client.post(
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            {"name": "Orchestra", "amount": "75.00", "currency": "USD", "section": ""},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PriceTier.objects.filter(name="Orchestra").exists())

        resp = self.client.post(
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            {"name": "Orchestra", "amount": "75.00", "currency": "USD", "section": section.pk},
            HTTP_HOST=host_for("roxy"),
        )
        tier = PriceTier.objects.get(name="Orchestra")
        self.assertEqual(tier.section_id, section.pk)
        self.assertIsNone(tier.performance_id)


class OverviewReportTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner, self.password = self.make_staff(self.org, Membership.Role.OWNER)
        self.client.force_login(self.owner)

    def test_gross_revenue_only_counts_paid_orders(self):
        _event, performance, _tier = self.build_ga_event(self.org, self.venue)
        self.make_paid_order(self.org, performance, "20.00")
        self.make_paid_order(self.org, performance, "40.00")
        # Not paid -- should not count.
        Order.objects.create(
            organization=self.org,
            performance=performance,
            buyer_email="pending@example.com",
            total=Decimal("999.00"),
            status=Order.Status.PENDING,
        )

        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["gross_revenue"], Decimal("60.00"))

    def test_tickets_sold_excludes_void(self):
        _event, performance, _tier = self.build_ga_event(self.org, self.venue)
        order = self.make_paid_order(self.org, performance, "20.00", n_tickets=2)
        void_ticket = order.tickets.first()
        void_ticket.status = Ticket.Status.VOID
        void_ticket.save(update_fields=["status"])

        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["tickets_sold"], 1)

    def test_per_performance_sold_and_capacity(self):
        _event, ga_perf, _tier = self.build_ga_event(self.org, self.venue, capacity=100, sold=0)
        self.make_paid_order(self.org, ga_perf, "20.00", n_tickets=3)

        _rev_event, rev_perf, _section, seats = self.build_reserved_event(self.org, self.venue, n_seats=4)
        order = Order.objects.create(
            organization=self.org, performance=rev_perf, buyer_email="x@example.com",
            total=Decimal("50.00"), status=Order.Status.PAID,
        )
        Ticket.objects.create(organization=self.org, order=order, performance=rev_perf, seat=seats[0])

        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        rows = {row["performance"].pk: row for row in resp.context["performance_rows"]}

        self.assertEqual(rows[ga_perf.pk]["sold"], 3)
        self.assertEqual(rows[ga_perf.pk]["capacity"], 100)
        self.assertEqual(rows[rev_perf.pk]["sold"], 1)
        self.assertEqual(rows[rev_perf.pk]["capacity"], 4)

    def test_upcoming_performances_excludes_past(self):
        _event, future_perf, _tier = self.build_ga_event(self.org, self.venue)
        past_event = Event.objects.create(organization=self.org, title="Past", slug="past")
        Performance.objects.create(
            organization=self.org,
            event=past_event,
            venue=self.venue,
            starts_at=timezone.now() - timedelta(days=1),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
        )
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        titles = [p.event.title for p in resp.context["upcoming_performances"]]
        self.assertIn("GA Show", titles)
        self.assertNotIn("Past", titles)
