"""Dashboard HTTP-layer tests: role gating for each area (any-staff /
manager+ / box-office+), tenant isolation on every list/detail view, CRUD
correctness (including that organization can never be spoofed via POST
data), and the overview report numbers."""

import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from events.models import Event, GAAllocation, Performance, PriceTier
from orders.models import Order, Ticket
from venues.generation import generate_seats
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


class ChartBuilderRoleGateTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """Chart-builder pages (Phase A, docs/SEATING.md) are manager+, exactly
    like event/performance CRUD -- see accounts.permissions.manager_required
    and ManagerRequiredMixin usage in dashboard/views.py."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.section = Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra")
        self.roles = {
            "owner": self.make_staff(self.org, Membership.Role.OWNER)[0],
            "manager": self.make_staff(self.org, Membership.Role.MANAGER)[0],
            "box_office": self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0],
            "scanner": self.make_staff(self.org, Membership.Role.SCANNER)[0],
        }

    def _login_as(self, role):
        self.client.logout()
        self.client.force_login(self.roles[role])

    def test_venue_list_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self.client.get("/dashboard/venues/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> venue list")

    def test_chart_list_manager_and_above_only(self):
        for role, expected in [("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self.client.get(
                f"/dashboard/venues/{self.venue.pk}/charts/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, expected, f"{role} -> chart list")

    def test_chart_detail_manager_and_above_only(self):
        for role, expected in [("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self.client.get(f"/dashboard/charts/{self.chart.pk}/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> chart detail")

    def test_section_detail_manager_and_above_only(self):
        for role, expected in [("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self.client.get(
                f"/dashboard/charts/{self.chart.pk}/sections/{self.section.pk}/",
                HTTP_HOST=host_for("roxy"),
            )
            self.assertEqual(resp.status_code, expected, f"{role} -> section detail")

    def test_generate_seats_manager_and_above_only(self):
        for role, expected in [("box_office", 403), ("scanner", 403), ("manager", 302)]:
            self._login_as(role)
            resp = self.client.post(
                f"/dashboard/charts/{self.chart.pk}/sections/{self.section.pk}/",
                {"rows": "2", "seats_per_row": "3"},
                HTTP_HOST=host_for("roxy"),
            )
            self.assertEqual(resp.status_code, expected, f"{role} -> generate seats")

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/dashboard/venues/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])


class ChartBuilderTenantIsolationTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org_a, self.venue_a = self.build_org("org-a")
        self.org_b, self.venue_b = self.build_org("org-b")
        self.chart_b = SeatingChart.objects.create(
            organization=self.org_b, venue=self.venue_b, name="B's chart"
        )
        self.section_b = Section.objects.create(
            organization=self.org_b, chart=self.chart_b, name="B's Orchestra"
        )
        self.manager_a, _ = self.make_staff(self.org_a, Membership.Role.MANAGER)
        self.client.force_login(self.manager_a)

    def test_venue_list_never_leaks_other_orgs_venues(self):
        resp = self.client.get("/dashboard/venues/", HTTP_HOST=host_for("org-a"))
        content = resp.content.decode()
        # Both build_org() venues are named "Main Stage" -- assert on the
        # per-org link URL instead of the (identical) display name.
        self.assertNotIn(f"/dashboard/venues/{self.venue_b.pk}/charts/", content)

    def test_chart_list_cross_org_venue_404s(self):
        resp = self.client.get(
            f"/dashboard/venues/{self.venue_b.pk}/charts/", HTTP_HOST=host_for("org-a")
        )
        self.assertEqual(resp.status_code, 404)

    def test_chart_detail_cross_org_404s(self):
        resp = self.client.get(f"/dashboard/charts/{self.chart_b.pk}/", HTTP_HOST=host_for("org-a"))
        self.assertEqual(resp.status_code, 404)

    def test_section_detail_cross_org_404s(self):
        resp = self.client.get(
            f"/dashboard/charts/{self.chart_b.pk}/sections/{self.section_b.pk}/",
            HTTP_HOST=host_for("org-a"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_cannot_generate_seats_on_another_orgs_section(self):
        resp = self.client.post(
            f"/dashboard/charts/{self.chart_b.pk}/sections/{self.section_b.pk}/",
            {"rows": "2", "seats_per_row": "3"},
            HTTP_HOST=host_for("org-a"),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.section_b.seats.count(), 0)

    def test_cannot_toggle_or_delete_another_orgs_seat(self):
        from venues.generation import generate_seats

        seats = generate_seats(self.section_b, [2])
        seat = seats[0]

        resp = self.client.post(
            f"/dashboard/charts/{self.chart_b.pk}/sections/{self.section_b.pk}/seats/{seat.pk}/toggle-accessible/",
            HTTP_HOST=host_for("org-a"),
        )
        self.assertEqual(resp.status_code, 404)
        seat.refresh_from_db()
        self.assertFalse(seat.is_accessible)

        resp = self.client.post(
            f"/dashboard/charts/{self.chart_b.pk}/sections/{self.section_b.pk}/seats/{seat.pk}/delete/",
            HTTP_HOST=host_for("org-a"),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Seat.objects.filter(pk=seat.pk).exists())


class ChartBuilderFlowTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """End-to-end usability check: create a chart, add a section with
    layout/numbering params, generate its seats, toggle accessible, remove a
    seat -- the full path a manager needs to build a real house."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.manager, _ = self.make_staff(self.org, Membership.Role.MANAGER)
        self.client.force_login(self.manager)

    def test_create_chart(self):
        resp = self.client.post(
            f"/dashboard/venues/{self.venue.pk}/charts/new/",
            {"name": "Standard house"},
            HTTP_HOST=host_for("roxy"),
        )
        chart = SeatingChart.objects.get(organization=self.org, venue=self.venue, name="Standard house")
        self.assertRedirects(
            resp, f"/dashboard/charts/{chart.pk}/", fetch_redirect_response=False
        )

    def test_create_section_with_layout_params(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        resp = self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/new/",
            {
                "name": "Orchestra",
                "ordering": "0",
                "tier": "Orchestra",
                "numbering_scheme": Section.NumberingScheme.ODD_DESC_LEFT,
                "row_label_scheme": Section.RowLabelScheme.SKIP_IO,
                "origin_x": "0",
                "origin_y": "0",
                "rotation": "0",
                "seat_pitch": "1",
                "row_pitch": "1",
                "row_x_offset": "0",
                "arc_radius": "",
            },
            HTTP_HOST=host_for("roxy"),
        )
        section = Section.objects.get(organization=self.org, chart=chart, name="Orchestra")
        self.assertEqual(section.numbering_scheme, Section.NumberingScheme.ODD_DESC_LEFT)
        self.assertEqual(section.tier, "Orchestra")
        self.assertRedirects(
            resp,
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            fetch_redirect_response=False,
        )

    def test_generate_seats_uniform_grid(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        resp = self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            {"rows": "3", "seats_per_row": "5"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp,
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            fetch_redirect_response=False,
        )
        self.assertEqual(section.seats.count(), 15)
        self.assertEqual(set(section.seats.values_list("row_label", flat=True)), {"A", "B", "C"})

    def test_generate_seats_ragged_rows(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        resp = self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            {"ragged_counts": "10,10,8"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp,
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            fetch_redirect_response=False,
        )
        self.assertEqual(section.seats.count(), 28)

    def test_regenerate_without_replace_shows_error_and_keeps_seats(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            {"rows": "1", "seats_per_row": "3"},
            HTTP_HOST=host_for("roxy"),
        )
        resp = self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            {"rows": "1", "seats_per_row": "5"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already has")
        self.assertEqual(section.seats.count(), 3)

    def test_regenerate_with_replace_rebuilds(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            {"rows": "1", "seats_per_row": "3"},
            HTTP_HOST=host_for("roxy"),
        )
        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/",
            {"rows": "1", "seats_per_row": "5", "replace_existing": "on"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(section.seats.count(), 5)

    def test_toggle_accessible(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        seat = Seat.objects.create(organization=self.org, section=section, row_label="A", number="1")

        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/seats/{seat.pk}/toggle-accessible/",
            HTTP_HOST=host_for("roxy"),
        )
        seat.refresh_from_db()
        self.assertTrue(seat.is_accessible)

        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/seats/{seat.pk}/toggle-accessible/",
            HTTP_HOST=host_for("roxy"),
        )
        seat.refresh_from_db()
        self.assertFalse(seat.is_accessible)

    def test_delete_seat_removes_it(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        seat = Seat.objects.create(organization=self.org, section=section, row_label="A", number="1")

        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/seats/{seat.pk}/delete/",
            HTTP_HOST=host_for("roxy"),
        )
        self.assertFalse(Seat.objects.filter(pk=seat.pk).exists())

    def test_delete_seat_with_live_ticket_refused(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        seat = Seat.objects.create(organization=self.org, section=section, row_label="A", number="1")
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        order = Order.objects.create(
            organization=self.org, performance=performance, buyer_email="x@example.com", total=Decimal("10.00")
        )
        Ticket.objects.create(organization=self.org, order=order, performance=performance, seat=seat)

        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/seats/{seat.pk}/delete/",
            HTTP_HOST=host_for("roxy"),
        )
        self.assertTrue(Seat.objects.filter(pk=seat.pk).exists())

    def test_performance_form_offers_seating_chart_field(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        resp = self.client.post(
            f"/dashboard/events/{event.pk}/performances/new/",
            {
                "venue": self.venue.pk,
                "starts_at": "2030-01-01T19:00",
                "seating_mode": Performance.SeatingMode.RESERVED,
                "status": Performance.Status.DRAFT,
                "seating_chart": chart.pk,
            },
            HTTP_HOST=host_for("roxy"),
        )
        performance = Performance.objects.get(event=event)
        self.assertEqual(performance.seating_chart_id, chart.pk)
        self.assertRedirects(
            resp, f"/dashboard/events/{event.pk}/", fetch_redirect_response=False
        )

    def test_editor_edit_layout_link_has_next_editor(self):
        # "Edit layout" from section_detail keeps the plain flow (back to
        # section_detail); the editor's own "Edit layout" link appends
        # ?next=editor so saving comes back here -- see
        # SectionUpdateView.get_success_url.
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        resp = self.client.get(f"/dashboard/charts/{chart.pk}/editor/", HTTP_HOST=host_for("roxy"))
        self.assertContains(
            resp, f"/dashboard/charts/{chart.pk}/sections/{section.pk}/edit/?next=editor"
        )

    def test_performance_form_rejects_chart_from_a_different_venue(self):
        other_venue = Venue.objects.create(organization=self.org, name="Second Stage")
        other_chart = SeatingChart.objects.create(
            organization=self.org, venue=other_venue, name="Other house"
        )
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        resp = self.client.post(
            f"/dashboard/events/{event.pk}/performances/new/",
            {
                "venue": self.venue.pk,
                "starts_at": "2030-01-01T19:00",
                "seating_mode": Performance.SeatingMode.RESERVED,
                "status": Performance.Status.DRAFT,
                "seating_chart": other_chart.pk,
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Performance.objects.filter(event=event).exists())


class ChartEditorTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """Phase B (docs/SEATING.md "B. Geometry + visual editor"): the SVG
    drag editor page, its batch save endpoint (manager-gated, org-scoped,
    never touches another tenant's seats, allowed even on ticketed seats),
    and the "regenerate seats" action's guardrails."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.section = Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra")
        self.seats = generate_seats(self.section, [3, 3])
        self.roles = {
            "owner": self.make_staff(self.org, Membership.Role.OWNER)[0],
            "manager": self.make_staff(self.org, Membership.Role.MANAGER)[0],
            "box_office": self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0],
            "scanner": self.make_staff(self.org, Membership.Role.SCANNER)[0],
        }

    def _login_as(self, role):
        self.client.logout()
        self.client.force_login(self.roles[role])

    def _post_json(self, url, payload, **extra):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json", **extra
        )

    # -- editor page: role gate + renders seats ----------------------------

    def test_editor_page_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self.client.get(f"/dashboard/charts/{self.chart.pk}/editor/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> chart editor")

    def test_editor_anonymous_redirected_to_login(self):
        resp = self.client.get(f"/dashboard/charts/{self.chart.pk}/editor/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_editor_cross_org_chart_404s(self):
        self._login_as("manager")
        other_chart = SeatingChart.objects.create(
            organization=self.other_org, venue=self.other_venue, name="Other"
        )
        resp = self.client.get(f"/dashboard/charts/{other_chart.pk}/editor/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 404)

    def test_editor_renders_every_seat_as_svg_circle(self):
        self._login_as("manager")
        resp = self.client.get(f"/dashboard/charts/{self.chart.pk}/editor/", HTTP_HOST=host_for("roxy"))
        content = resp.content.decode()
        self.assertContains(resp, "<svg")
        self.assertEqual(content.count("<circle"), 6)
        for seat in self.seats:
            self.assertIn(f'data-seat-id="{seat.pk}"', content)
            self.assertIn(f'data-section-id="{self.section.pk}"', content)

    # -- save endpoint: role gate ------------------------------------------

    def test_save_manager_and_above_only(self):
        seat = self.seats[0]
        payload = {"positions": {str(seat.pk): {"x": 5.0, "y": 6.0}}}
        for role, expected in [("box_office", 403), ("scanner", 403), ("owner", 200), ("manager", 200)]:
            self._login_as(role)
            resp = self._post_json(
                f"/dashboard/charts/{self.chart.pk}/editor/save/", payload, HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, expected, f"{role} -> save")

    def test_save_anonymous_redirected_to_login(self):
        seat = self.seats[0]
        resp = self._post_json(
            f"/dashboard/charts/{self.chart.pk}/editor/save/",
            {"positions": {str(seat.pk): {"x": 1.0, "y": 2.0}}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    # -- save endpoint: persists x/y -----------------------------------

    def test_save_persists_dragged_positions(self):
        self._login_as("manager")
        seat_a, seat_b = self.seats[0], self.seats[1]
        resp = self._post_json(
            f"/dashboard/charts/{self.chart.pk}/editor/save/",
            {"positions": {str(seat_a.pk): {"x": 12.5, "y": -3.25}, str(seat_b.pk): {"x": 0.0, "y": 0.0}}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["updated"], 2)
        seat_a.refresh_from_db()
        seat_b.refresh_from_db()
        self.assertEqual(seat_a.x, 12.5)
        self.assertEqual(seat_a.y, -3.25)
        self.assertEqual(seat_b.x, 0.0)
        self.assertEqual(seat_b.y, 0.0)

    def test_save_works_on_a_ticketed_seat(self):
        # Repositioning is cosmetic (position != identity) -- unlike
        # seat_delete/generate_seats, this is allowed even for a seat
        # backing a live ticket.
        self._login_as("manager")
        seat = self.seats[0]
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now() + timedelta(days=1),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        order = Order.objects.create(
            organization=self.org, performance=performance, buyer_email="x@example.com", total=Decimal("10.00")
        )
        Ticket.objects.create(organization=self.org, order=order, performance=performance, seat=seat)

        resp = self._post_json(
            f"/dashboard/charts/{self.chart.pk}/editor/save/",
            {"positions": {str(seat.pk): {"x": 99.0, "y": 99.0}}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["updated"], 1)
        seat.refresh_from_db()
        self.assertEqual(seat.x, 99.0)
        self.assertEqual(seat.y, 99.0)

    # -- save endpoint: tenant isolation -------------------------------

    def test_save_cannot_move_another_orgs_seat(self):
        other_chart = SeatingChart.objects.create(
            organization=self.other_org, venue=self.other_venue, name="Other"
        )
        other_section = Section.objects.create(
            organization=self.other_org, chart=other_chart, name="Other Orchestra"
        )
        other_seat = generate_seats(other_section, [1])[0]
        original_x, original_y = other_seat.x, other_seat.y

        self._login_as("manager")  # manager of self.org, NOT other_org
        resp = self._post_json(
            f"/dashboard/charts/{self.chart.pk}/editor/save/",
            {"positions": {str(other_seat.pk): {"x": 500.0, "y": 500.0}}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["updated"], 0)  # nothing mutated
        other_seat.refresh_from_db()
        self.assertEqual(other_seat.x, original_x)
        self.assertEqual(other_seat.y, original_y)

    def test_save_via_another_orgs_chart_url_404s(self):
        # Posting to org A's manager session but org B's chart pk in the
        # URL -- get_object_or_404(..., organization=request.organization)
        # on the chart itself is the first gate.
        other_chart = SeatingChart.objects.create(
            organization=self.other_org, venue=self.other_venue, name="Other"
        )
        other_section = Section.objects.create(
            organization=self.other_org, chart=other_chart, name="Other Orchestra"
        )
        other_seat = generate_seats(other_section, [1])[0]

        self._login_as("manager")
        resp = self._post_json(
            f"/dashboard/charts/{other_chart.pk}/editor/save/",
            {"positions": {str(other_seat.pk): {"x": 1.0, "y": 1.0}}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)
        other_seat.refresh_from_db()
        self.assertNotEqual((other_seat.x, other_seat.y), (1.0, 1.0))

    def test_save_rejects_bad_payload_shapes(self):
        self._login_as("manager")
        for bad_payload in [{}, {"positions": []}, {"positions": {}}, {"positions": {"abc": {"x": 1, "y": 2}}}]:
            resp = self._post_json(
                f"/dashboard/charts/{self.chart.pk}/editor/save/", bad_payload, HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, 400, f"payload {bad_payload!r}")

    # -- regenerate action ------------------------------------------------

    def test_regenerate_manager_and_above_only(self):
        for role, expected in [("box_office", 403), ("scanner", 403), ("manager", 302)]:
            self._login_as(role)
            resp = self.client.post(
                f"/dashboard/charts/{self.chart.pk}/sections/{self.section.pk}/regenerate/",
                HTTP_HOST=host_for("roxy"),
            )
            self.assertEqual(resp.status_code, expected, f"{role} -> regenerate")

    def test_regenerate_applies_new_layout_params_to_same_row_shape(self):
        self._login_as("manager")
        self.section.rotation = 0
        self.section.origin_x = 100.0
        self.section.save(update_fields=["rotation", "origin_x"])

        resp = self.client.post(
            f"/dashboard/charts/{self.chart.pk}/sections/{self.section.pk}/regenerate/",
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp, f"/dashboard/charts/{self.chart.pk}/editor/", fetch_redirect_response=False
        )
        seats = list(self.section.seats.all())
        self.assertEqual(len(seats), 6)  # same row shape: [3, 3]
        self.assertTrue(all(s.x >= 100.0 for s in seats))

    def test_regenerate_refuses_when_live_ticket_exists(self):
        self._login_as("manager")
        seat = self.seats[0]
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now() + timedelta(days=1),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        order = Order.objects.create(
            organization=self.org, performance=performance, buyer_email="x@example.com", total=Decimal("10.00")
        )
        Ticket.objects.create(organization=self.org, order=order, performance=performance, seat=seat)

        resp = self.client.post(
            f"/dashboard/charts/{self.chart.pk}/sections/{self.section.pk}/regenerate/",
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp, f"/dashboard/charts/{self.chart.pk}/editor/", fetch_redirect_response=False
        )
        # Unchanged -- refused, not silently dropped.
        self.assertEqual(self.section.seats.count(), 6)
        seat.refresh_from_db()

    def test_regenerate_cross_org_section_404s(self):
        other_chart = SeatingChart.objects.create(
            organization=self.other_org, venue=self.other_venue, name="Other"
        )
        other_section = Section.objects.create(
            organization=self.other_org, chart=other_chart, name="Other Orchestra"
        )
        generate_seats(other_section, [2])

        self._login_as("manager")
        resp = self.client.post(
            f"/dashboard/charts/{other_chart.pk}/sections/{other_section.pk}/regenerate/",
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(other_section.seats.count(), 2)
