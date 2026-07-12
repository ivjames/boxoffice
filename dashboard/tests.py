"""Dashboard HTTP-layer tests: role gating for each area (any-staff /
manager+ / box-office+), tenant isolation on every list/detail view, CRUD
correctness (including that organization can never be spoofed via POST
data), and the overview report numbers."""

import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from unittest.mock import patch

from django.core import mail

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from events.models import Event, GAAllocation, Performance, PriceTier, PricingZone, ZoneTemplate
from orders.models import Order, Payment, Ticket
from orders.services import get_seating_chart
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

    def test_overview_open_to_box_office_and_above(self):
        # Scanners work the door only -- the overview isn't theirs, so a
        # scanner who lands on it is bounced to the scan screen.
        for role in ["owner", "manager", "box_office"]:
            self.client.logout()
            self._login_as(role)
            resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, 200, f"{role} should reach the overview")

    def test_scanner_overview_redirects_to_scan(self):
        self._login_as("scanner")
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse("scan_home"))

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

    def test_reserved_editor_lists_every_section_including_unpriced(self):
        _event, performance, section, _seats = self.build_reserved_event(self.org, self.venue)
        chart = get_seating_chart(performance)
        balcony = Section.objects.create(organization=self.org, chart=chart, name="Balcony")
        resp = self.client.get(
            f"/dashboard/performances/{performance.pk}/price-tiers/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        section_ids = {row["section"].id for row in resp.context["rows"]}
        self.assertEqual(section_ids, {section.id, balcony.id})
        # An unpriced section is shown, not silently omitted.
        self.assertContains(resp, "Not priced")

    def test_reserved_editor_sets_section_default(self):
        _event, performance, section, _seats = self.build_reserved_event(self.org, self.venue)
        resp = self.client.post(
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            {f"default_{section.id}": "45.00", f"override_{section.id}": ""},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp,
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            fetch_redirect_response=False,
        )
        tier = PriceTier.objects.get(section=section, performance__isnull=True)
        self.assertEqual(tier.amount, Decimal("45.00"))
        self.assertIsNone(tier.performance_id)
        self.assertEqual(tier.organization_id, self.org.id)

    def test_reserved_editor_sets_override_separately_from_default(self):
        _event, performance, section, _seats = self.build_reserved_event(self.org, self.venue)
        self.client.post(
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            {f"default_{section.id}": "45.00", f"override_{section.id}": "85.00"},
            HTTP_HOST=host_for("roxy"),
        )
        default = PriceTier.objects.get(section=section, performance__isnull=True)
        override = PriceTier.objects.get(section=section, performance=performance)
        self.assertEqual(default.amount, Decimal("45.00"))
        self.assertEqual(override.amount, Decimal("85.00"))

    def test_reserved_editor_blank_clears_existing_price(self):
        _event, performance, section, _seats = self.build_reserved_event(self.org, self.venue)
        PriceTier.objects.create(
            organization=self.org, section=section, name="Orchestra", amount=Decimal("45.00")
        )
        resp = self.client.post(
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            {f"default_{section.id}": "", f"override_{section.id}": ""},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp,
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            fetch_redirect_response=False,
        )
        self.assertFalse(
            PriceTier.objects.filter(section=section, performance__isnull=True).exists()
        )

    def test_reserved_editor_rejects_negative_and_saves_nothing(self):
        _event, performance, section, _seats = self.build_reserved_event(self.org, self.venue)
        resp = self.client.post(
            f"/dashboard/performances/{performance.pk}/price-tiers/",
            {f"default_{section.id}": "-5", f"override_{section.id}": ""},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "can&#x27;t be negative")
        self.assertFalse(PriceTier.objects.filter(section=section).exists())


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


class OverviewRevenueGateTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """Revenue is manager+ only: box office sells tickets and services the
    door, it doesn't need the money reports (see dashboard.views.overview)."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        _event, self.performance, _tier = self.build_ga_event(self.org, self.venue)
        self.make_paid_order(self.org, self.performance, "60.00")

    def test_box_office_overview_hides_revenue(self):
        user = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.client.force_login(user)
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["show_revenue"])
        self.assertNotContains(resp, "Gross revenue")
        self.assertNotContains(resp, "Revenue by event")

    def test_manager_overview_shows_revenue(self):
        user = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(user)
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertTrue(resp.context["show_revenue"])
        self.assertContains(resp, "Gross revenue")
        self.assertEqual(resp.context["gross_revenue"], Decimal("60.00"))


class PerformanceDetailTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """The show-detail page linked from the overview's upcoming-shows list:
    seating chart + ticket summary + guest lookup, box_office-gated with
    revenue held to manager+ (dashboard.views.performance_detail)."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        _event, self.perf, self.section, self.seats = self.build_reserved_event(
            self.org, self.venue, n_seats=3
        )
        self.order = Order.objects.create(
            organization=self.org, performance=self.perf, buyer_email="jo@example.com",
            buyer_name="Jo Buyer", total=Decimal("25.00"), status=Order.Status.PAID,
        )
        self.ticket = Ticket.objects.create(
            organization=self.org, order=self.order, performance=self.perf,
            seat=self.seats[0], holder_name="Guest One",
        )

    def _url(self):
        return reverse("dashboard_performance_detail", args=[self.perf.pk])

    def test_box_office_and_above_can_view_scanner_cannot(self):
        for role, expected in [
            (Membership.Role.OWNER, 200),
            (Membership.Role.MANAGER, 200),
            (Membership.Role.BOX_OFFICE, 200),
            (Membership.Role.SCANNER, 403),
        ]:
            self.client.logout()
            self.client.force_login(self.make_staff(self.org, role)[0])
            resp = self.client.get(self._url(), HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> performance detail")

    def test_summary_counts_and_seat_states(self):
        self.client.force_login(self.make_staff(self.org, Membership.Role.OWNER)[0])
        resp = self.client.get(self._url(), HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["sold"], 1)
        self.assertEqual(resp.context["capacity"], 3)
        states = {s["id"]: s["state"] for s in resp.context["seats_json"]}
        self.assertEqual(states[self.seats[0].id], "sold")
        self.assertEqual(states[self.seats[1].id], "available")

    def test_revenue_hidden_from_box_office(self):
        self.client.force_login(self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0])
        resp = self.client.get(self._url(), HTTP_HOST=host_for("roxy"))
        self.assertFalse(resp.context["show_revenue"])
        self.assertIsNone(resp.context["revenue"])

    def test_guest_search_finds_ticket_by_holder_name(self):
        self.client.force_login(self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0])
        resp = self.client.get(self._url() + "?q=Guest One", HTTP_HOST=host_for("roxy"))
        self.assertEqual([t.pk for t in resp.context["search_results"]], [self.ticket.pk])
        self.assertContains(resp, self.ticket.token)

    def test_guest_search_by_buyer_email(self):
        self.client.force_login(self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0])
        resp = self.client.get(self._url() + "?q=jo@example.com", HTTP_HOST=host_for("roxy"))
        self.assertEqual([t.pk for t in resp.context["search_results"]], [self.ticket.pk])

    def test_guest_search_no_match(self):
        self.client.force_login(self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0])
        resp = self.client.get(self._url() + "?q=nobody", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["search_results"], [])

    def test_cross_org_performance_404s(self):
        other_org, other_venue = self.build_org("other")
        _e, other_perf, _s, _seats = self.build_reserved_event(other_org, other_venue, slug="x")
        self.client.force_login(self.make_staff(self.org, Membership.Role.OWNER)[0])
        resp = self.client.get(
            reverse("dashboard_performance_detail", args=[other_perf.pk]),
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)


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

    def test_cannot_save_another_orgs_section_via_the_live_editor(self):
        original_origin_x = self.section_b.origin_x
        resp = self.client.post(
            f"/dashboard/charts/{self.chart_b.pk}/editor/save/",
            data=json.dumps({"sections": {str(self.section_b.pk): {"origin_x": 999.0}}}),
            content_type="application/json",
            HTTP_HOST=host_for("org-a"),
        )
        # 404 -- get_object_or_404 on the chart itself (org-a has no access
        # to org-b's chart at all) gates before the section lookup.
        self.assertEqual(resp.status_code, 404)
        self.section_b.refresh_from_db()
        self.assertEqual(self.section_b.origin_x, original_origin_x)


class ChartBuilderFlowTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """End-to-end usability check: create a chart, add a section -- the
    section-shell creation path a manager needs before shaping/placing it
    live in the chart editor (dashboard/tests.py's ChartEditorTests covers
    the live editor's save flow itself)."""

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

    def test_create_section_redirects_into_the_live_editor(self):
        # Layout params (origin/pitch/rotation/offset/arc/shape) are no
        # longer set via this form -- docs/EDITOR.md moves them to the live
        # chart editor -- so creating a section only takes name/tier/
        # numbering, and success lands staff straight in the editor with
        # the new section selected, ready to shape/place it.
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        resp = self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/new/",
            {
                "name": "Orchestra",
                "ordering": "0",
                "tier": "Orchestra",
                "numbering_scheme": Section.NumberingScheme.ODD_DESC_LEFT,
                "row_label_scheme": Section.RowLabelScheme.SKIP_IO,
            },
            HTTP_HOST=host_for("roxy"),
        )
        section = Section.objects.get(organization=self.org, chart=chart, name="Orchestra")
        self.assertEqual(section.numbering_scheme, Section.NumberingScheme.ODD_DESC_LEFT)
        self.assertEqual(section.tier, "Orchestra")
        self.assertRedirects(
            resp,
            f"/dashboard/charts/{chart.pk}/editor/?section={section.pk}",
            fetch_redirect_response=False,
        )

    def test_second_section_gets_a_staggered_default_origin(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/new/",
            {"name": "Balcony", "tier": "", "numbering_scheme": "sequential", "row_label_scheme": "skip_io"},
            HTTP_HOST=host_for("roxy"),
        )
        balcony = Section.objects.get(organization=self.org, chart=chart, name="Balcony")
        self.assertEqual(balcony.origin_x, 12.0)

    def test_new_section_ordering_is_auto_assigned_not_a_form_field(self):
        # Round 2 feedback on docs/EDITOR.md #7: "ordering" is no longer a
        # raw sort-number input on the create form (see SectionForm's
        # docstring) -- a new section is auto-appended to the end of the
        # chart's current list instead, same staggering idea as origin_x.
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        orchestra = Section.objects.create(organization=self.org, chart=chart, name="Orchestra", ordering=0)
        resp = self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/new/",
            {
                "name": "Balcony", "ordering": "999", "tier": "",
                "numbering_scheme": "sequential", "row_label_scheme": "skip_io",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 302)
        balcony = Section.objects.get(organization=self.org, chart=chart, name="Balcony")
        # The posted "ordering": "999" is silently ignored (not a form
        # field) -- the section is appended right after the one existing
        # section, not moved to some arbitrary value a client could send.
        self.assertEqual(balcony.ordering, 1)
        orchestra.refresh_from_db()
        self.assertEqual(orchestra.ordering, 0)

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

    def test_section_update_view_edits_metadata_and_returns_to_chart_detail(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        resp = self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/edit/",
            {
                "name": "Orchestra Center",
                "ordering": "0",
                "tier": "Premium",
                "numbering_scheme": "sequential",
                "row_label_scheme": "skip_io",
            },
            HTTP_HOST=host_for("roxy"),
        )
        section.refresh_from_db()
        self.assertEqual(section.name, "Orchestra Center")
        self.assertEqual(section.tier, "Premium")
        self.assertRedirects(
            resp, f"/dashboard/charts/{chart.pk}/", fetch_redirect_response=False
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


class InlineSectionCreateTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """docs/EDITOR.md's Round 2 refinement #7: "New section" must not
    navigate away from the editor. Covers the AJAX path of
    SectionCreateView (X-Requested-With: XMLHttpRequest) -- the plain-POST
    redirect path is already covered by ChartBuilderFlowTests above; this
    class is about the JSON response chart_editor.js's inline modal relies
    on, plus that it's still manager-gated and org-/chart-scoped."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.roles = {
            "owner": self.make_staff(self.org, Membership.Role.OWNER)[0],
            "manager": self.make_staff(self.org, Membership.Role.MANAGER)[0],
            "box_office": self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0],
            "scanner": self.make_staff(self.org, Membership.Role.SCANNER)[0],
        }

    def _login_as(self, role):
        self.client.logout()
        self.client.force_login(self.roles[role])

    def _ajax_post(self, chart, data, **extra):
        return self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/new/",
            data,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            **extra,
        )

    def test_ajax_create_returns_json_shaped_for_the_live_editor(self):
        self._login_as("manager")
        resp = self._ajax_post(
            self.chart,
            {
                "name": "Orchestra",
                "ordering": "0",
                "tier": "Premium",
                "numbering_scheme": "sequential",
                "row_label_scheme": "skip_io",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        section = Section.objects.get(organization=self.org, chart=self.chart, name="Orchestra")
        payload = data["section"]
        self.assertEqual(payload["id"], section.pk)
        self.assertEqual(payload["name"], "Orchestra")
        self.assertEqual(payload["tier"], "Premium")
        self.assertIn("color", payload)
        self.assertIn("edit_url", payload)
        # Same param fields chart_editor()'s initial json_script payload
        # ships -- makeSection() (chart_editor.js) treats the two
        # identically.
        self.assertEqual(payload["rows"], section.rows)
        self.assertEqual(payload["pivot_mode"], "center")
        self.assertEqual(payload["removed_seats"], [])
        # No page navigation -- this is a JSON response, not a redirect.
        self.assertEqual(resp["Content-Type"], "application/json")

    def test_ajax_create_does_not_navigate_the_non_ajax_path_still_redirects(self):
        # Sanity check that adding the AJAX branch didn't change the plain
        # form-POST behavior other tests (ChartBuilderFlowTests) rely on.
        self._login_as("manager")
        resp = self.client.post(
            f"/dashboard/charts/{self.chart.pk}/sections/new/",
            {
                "name": "Balcony", "ordering": "0", "tier": "",
                "numbering_scheme": "sequential", "row_label_scheme": "skip_io",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 302)

    def test_ajax_create_invalid_name_returns_json_errors_not_html(self):
        self._login_as("manager")
        Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra")
        resp = self._ajax_post(
            self.chart,
            {
                "name": "Orchestra",  # duplicate -- unique_section_name_per_chart
                "ordering": "0", "tier": "", "numbering_scheme": "sequential",
                "row_label_scheme": "skip_io",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("name", data["errors"])
        self.assertEqual(Section.objects.filter(organization=self.org, chart=self.chart).count(), 1)

    def test_ajax_create_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self._ajax_post(
                self.chart,
                {
                    "name": f"Section-{role}", "ordering": "0", "tier": "",
                    "numbering_scheme": "sequential", "row_label_scheme": "skip_io",
                },
                HTTP_HOST=host_for("roxy"),
            )
            self.assertEqual(resp.status_code, expected, f"{role} -> inline section create")

    def test_ajax_create_anonymous_redirected_to_login(self):
        resp = self._ajax_post(
            self.chart,
            {
                "name": "Orchestra", "ordering": "0", "tier": "",
                "numbering_scheme": "sequential", "row_label_scheme": "skip_io",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_ajax_create_cannot_target_another_orgs_chart(self):
        other_chart = SeatingChart.objects.create(
            organization=self.other_org, venue=self.other_venue, name="Other"
        )
        self._login_as("manager")  # manager of self.org, NOT other_org
        resp = self._ajax_post(
            other_chart,
            {
                "name": "Orchestra", "ordering": "0", "tier": "",
                "numbering_scheme": "sequential", "row_label_scheme": "skip_io",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Section.objects.filter(organization=self.other_org, chart=other_chart).exists())


class SectionReorderTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """dashboard_section_reorder: the up/down-arrow swap that replaces a
    manual "ordering" number field on the section forms (Round 2 feedback
    on docs/EDITOR.md #7 -- see SectionForm's docstring)."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.first = Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra", ordering=0)
        self.second = Section.objects.create(organization=self.org, chart=self.chart, name="Balcony", ordering=1)
        self.third = Section.objects.create(organization=self.org, chart=self.chart, name="Mezzanine", ordering=2)
        self.roles = {
            "owner": self.make_staff(self.org, Membership.Role.OWNER)[0],
            "manager": self.make_staff(self.org, Membership.Role.MANAGER)[0],
            "box_office": self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0],
            "scanner": self.make_staff(self.org, Membership.Role.SCANNER)[0],
        }

    def _login_as(self, role):
        self.client.logout()
        self.client.force_login(self.roles[role])

    def _reorder(self, chart, section, direction, **extra):
        return self.client.post(
            f"/dashboard/charts/{chart.pk}/sections/{section.pk}/reorder/",
            data=json.dumps({"direction": direction}),
            content_type="application/json",
            **extra,
        )

    def test_move_down_swaps_with_next_neighbor(self):
        self._login_as("manager")
        resp = self._reorder(self.chart, self.first, "down", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["order"], [self.second.pk, self.first.pk, self.third.pk])
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.first.ordering, 1)
        self.assertEqual(self.second.ordering, 0)

    def test_move_up_swaps_with_previous_neighbor(self):
        self._login_as("manager")
        resp = self._reorder(self.chart, self.third, "up", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["order"], [self.first.pk, self.third.pk, self.second.pk])

    def test_move_up_on_first_section_is_a_no_op(self):
        self._login_as("manager")
        resp = self._reorder(self.chart, self.first, "up", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["order"], [self.first.pk, self.second.pk, self.third.pk])
        self.first.refresh_from_db()
        self.assertEqual(self.first.ordering, 0)

    def test_move_down_on_last_section_is_a_no_op(self):
        self._login_as("manager")
        resp = self._reorder(self.chart, self.third, "down", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["order"], [self.first.pk, self.second.pk, self.third.pk])

    def test_invalid_direction_400s(self):
        self._login_as("manager")
        resp = self._reorder(self.chart, self.first, "sideways", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 400)

    def test_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self._reorder(self.chart, self.first, "down", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> section reorder")

    def test_anonymous_redirected_to_login(self):
        resp = self._reorder(self.chart, self.first, "down", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_cannot_reorder_another_orgs_section(self):
        other_chart = SeatingChart.objects.create(
            organization=self.other_org, venue=self.other_venue, name="Other"
        )
        other_section = Section.objects.create(
            organization=self.other_org, chart=other_chart, name="Other Orchestra", ordering=0
        )
        original_ordering = other_section.ordering
        self._login_as("manager")  # manager of self.org, NOT other_org
        resp = self._reorder(other_chart, other_section, "down", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 404)
        other_section.refresh_from_db()
        self.assertEqual(other_section.ordering, original_ordering)


class ChartEditorTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """docs/EDITOR.md's live, param-driven chart editor: the editor page
    (ships every section's params as JSON, no server-side seat rendering --
    seats are computed live client-side), and its batch save endpoint
    (manager-gated, org-scoped, regenerates seats server-side via
    venues.generation with the SAME formulas the client just used, applies
    removed/accessible overrides, keeps the Phase-A live-ticket guard)."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.section = Section.objects.create(
            organization=self.org, chart=self.chart, name="Orchestra", rows=3, seats_per_row=3
        )
        self.seats = generate_seats(self.section, [3, 3, 3])
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

    def _save_url(self, chart=None):
        return f"/dashboard/charts/{(chart or self.chart).pk}/editor/save/"

    def _params_payload(self, section=None, **overrides):
        section = section or self.section
        payload = {
            "origin_x": section.origin_x,
            "origin_y": section.origin_y,
            "rotation": section.rotation,
            "seat_pitch": section.seat_pitch,
            "row_pitch": section.row_pitch,
            "row_x_offset": section.row_x_offset,
            "arc_radius": section.arc_radius,
            "offset_mode": section.offset_mode,
            "alt_row_seat_delta": section.alt_row_seat_delta,
            "rows": section.rows,
            "seats_per_row": section.seats_per_row,
            "numbering_scheme": section.numbering_scheme,
            "row_label_scheme": section.row_label_scheme,
            "pivot_mode": section.pivot_mode,
            "pivot_x": section.pivot_x,
            "pivot_y": section.pivot_y,
            "removed": [],
            "accessible": [],
        }
        payload.update(overrides)
        return payload

    # -- editor page: role gate + ships params (no server-rendered seats) --

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

    def test_editor_ships_section_params_as_json(self):
        self._login_as("manager")
        resp = self.client.get(f"/dashboard/charts/{self.chart.pk}/editor/", HTTP_HOST=host_for("roxy"))
        content = resp.content.decode()
        self.assertContains(resp, "<svg")
        # Round 2 (docs/EDITOR.md, "add sections without leaving the
        # editor") moves <g data-section-group> creation into
        # chart_editor.js's ensureSectionGroup() -- a section added inline
        # has no server-rendered group to bind to, so EVERY section's group
        # (including ones present at page load) is now created client-side.
        # No `data-section-group` attribute is server-rendered at all.
        self.assertNotIn("data-section-group", content)
        # The embedded json_script payload carries this section's shape --
        # not individual seats (those are computed live, client-side) --
        # including Round 2's configurable-pivot fields.
        self.assertContains(resp, '"rows": 3')
        self.assertContains(resp, '"seats_per_row": 3')
        self.assertContains(resp, '"pivot_mode": "center"')
        self.assertNotContains(resp, "editor-seat")  # no server-rendered <circle> seats

    # -- save endpoint: role gate ------------------------------------------

    def test_save_manager_and_above_only(self):
        payload = {"sections": {str(self.section.pk): self._params_payload()}}
        for role, expected in [("box_office", 403), ("scanner", 403), ("owner", 200), ("manager", 200)]:
            self._login_as(role)
            resp = self._post_json(self._save_url(), payload, HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> save")

    def test_save_anonymous_redirected_to_login(self):
        resp = self._post_json(
            self._save_url(), {"sections": {str(self.section.pk): self._params_payload()}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    # -- save endpoint: persists params + regenerates seats -----------------

    def test_save_persists_params_and_regenerates_seats(self):
        self._login_as("manager")
        resp = self._post_json(
            self._save_url(),
            {"sections": {str(self.section.pk): self._params_payload(rows=2, seats_per_row=4, origin_x=50.0)}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["saved"], [self.section.pk])
        self.section.refresh_from_db()
        self.assertEqual(self.section.rows, 2)
        self.assertEqual(self.section.seats_per_row, 4)
        self.assertEqual(self.section.origin_x, 50.0)
        seats = list(self.section.seats.all())
        self.assertEqual(len(seats), 8)
        self.assertTrue(all(s.x >= 50.0 for s in seats))

    def test_save_persists_custom_pivot(self):
        # Round 2 (docs/EDITOR.md #2): pivot_mode/pivot_x/pivot_y round-trip
        # through the same save endpoint as every other layout param, and
        # actually change generated geometry (rotation pivots on the
        # dragged point, not the section center/origin).
        self._login_as("manager")
        resp = self._post_json(
            self._save_url(),
            {
                "sections": {
                    str(self.section.pk): self._params_payload(
                        rows=1, seats_per_row=1, rotation=90.0,
                        pivot_mode="custom", pivot_x=5.0, pivot_y=0.0,
                    )
                }
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.section.refresh_from_db()
        self.assertEqual(self.section.pivot_mode, "custom")
        self.assertEqual(self.section.pivot_x, 5.0)
        self.assertEqual(self.section.pivot_y, 0.0)
        # origin_x/origin_y default to 0,0 -- with pivot (5, 0) and a
        # 90-degree turn, local (0, 0) (the only seat) swings to (5, -5).
        seat = self.section.seats.get()
        self.assertAlmostEqual(seat.x, 5.0, places=6)
        self.assertAlmostEqual(seat.y, -5.0, places=6)

    def test_save_applies_alternating_offset_and_alt_row_seat_delta(self):
        self._login_as("manager")
        resp = self._post_json(
            self._save_url(),
            {
                "sections": {
                    str(self.section.pk): self._params_payload(
                        rows=2, seats_per_row=3, offset_mode="alternating",
                        row_x_offset=0.5, alt_row_seat_delta=1,
                    )
                }
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        by_row = {}
        for seat in self.section.seats.all():
            by_row.setdefault(seat.row_label, []).append(seat)
        self.assertEqual(len(by_row["A"]), 3)
        self.assertEqual(len(by_row["B"]), 4)  # seats_per_row + alt_row_seat_delta
        self.assertEqual(sorted(s.x for s in by_row["A"]), [0.0, 1.0, 2.0])
        self.assertEqual(sorted(s.x for s in by_row["B"]), [0.5, 1.5, 2.5, 3.5])

    def test_save_clamps_alt_row_seat_delta_to_plus_minus_one(self):
        # Round 3 (docs/EDITOR.md #9): alt-row add/drop is a small
        # brick-stagger nudge -- the editor's stepper already clamps to
        # -1/0/+1 client-side, but the save endpoint is the authoritative
        # backstop against a stale/tampered client payload sending a bigger
        # delta straight through.
        self._login_as("manager")
        resp = self._post_json(
            self._save_url(),
            {
                "sections": {
                    str(self.section.pk): self._params_payload(
                        rows=2, seats_per_row=3, offset_mode="alternating",
                        row_x_offset=0.5, alt_row_seat_delta=7,
                    )
                }
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.section.refresh_from_db()
        self.assertEqual(self.section.alt_row_seat_delta, 1)

        resp = self._post_json(
            self._save_url(),
            {
                "sections": {
                    str(self.section.pk): self._params_payload(
                        rows=2, seats_per_row=3, offset_mode="alternating",
                        row_x_offset=0.5, alt_row_seat_delta=-9,
                    )
                }
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.section.refresh_from_db()
        self.assertEqual(self.section.alt_row_seat_delta, -1)

    def test_save_clamps_row_x_offset_to_plus_minus_two(self):
        # Round-4 correction (docs/EDITOR.md): the offset amount is capped
        # at +/-2 (round 3 had raised it much higher -- a misread of the
        # user's feedback) -- the editor's slider already clamps to that
        # range client-side, but the save endpoint is the authoritative
        # backstop against a stale/tampered client payload, same pattern as
        # alt_row_seat_delta's clamp test above.
        self._login_as("manager")
        resp = self._post_json(
            self._save_url(),
            {"sections": {str(self.section.pk): self._params_payload(row_x_offset=50.0)}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.section.refresh_from_db()
        self.assertEqual(self.section.row_x_offset, 2.0)

        resp = self._post_json(
            self._save_url(),
            {"sections": {str(self.section.pk): self._params_payload(row_x_offset=-50.0)}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.section.refresh_from_db()
        self.assertEqual(self.section.row_x_offset, -2.0)

    def test_save_applies_offset_composed_with_arc(self):
        # Round-4 correction (docs/EDITOR.md): offset must work TOGETHER
        # with arc (round 3 had disabled it) -- a single-seat-per-row
        # section isolates the offset contribution from arc's trig terms,
        # same trick venues/test_generation.py's contract tests use.
        self._login_as("manager")
        resp = self._post_json(
            self._save_url(),
            {
                "sections": {
                    str(self.section.pk): self._params_payload(
                        rows=2, seats_per_row=1, arc_radius=10.0, row_pitch=5.0,
                        row_x_offset=0.5, offset_mode="repeated",
                    )
                }
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        by = {(s.row_label, s.number): (s.x, s.y) for s in self.section.seats.all()}
        self.assertAlmostEqual(by[("A", "1")][0], 0.0, places=6)
        self.assertAlmostEqual(by[("B", "1")][0], 0.5, places=6)
        self.assertAlmostEqual(by[("B", "1")][1], 5.0, places=6)

    def test_save_persists_removed_and_accessible_overrides(self):
        self._login_as("manager")
        seat_a1 = self.section.seats.get(row_label="A", number="1")
        resp = self._post_json(
            self._save_url(),
            {
                "sections": {
                    str(self.section.pk): self._params_payload(
                        removed=[["A", "1"]], accessible=[["A", "2"]]
                    )
                }
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertFalse(Seat.objects.filter(pk=seat_a1.pk).exists())
        self.assertFalse(self.section.seats.filter(row_label="A", number="1").exists())
        self.assertTrue(self.section.seats.get(row_label="A", number="2").is_accessible)
        self.section.refresh_from_db()
        self.assertEqual(self.section.removed_seats, [["A", "1"]])
        self.assertEqual(self.section.accessible_seats, [["A", "2"]])

    def test_save_refuses_when_live_ticket_exists_and_leaves_section_untouched(self):
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
        original_origin_x = self.section.origin_x

        resp = self._post_json(
            self._save_url(),
            {"sections": {str(self.section.pk): self._params_payload(origin_x=500.0, rows=1)}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn(str(self.section.pk), data["errors"])
        # Refused, not silently applied -- neither the params nor the seats
        # changed.
        self.section.refresh_from_db()
        self.assertEqual(self.section.origin_x, original_origin_x)
        self.assertEqual(self.section.seats.count(), 9)  # unchanged: [3, 3, 3]

    def test_save_moves_section_in_place_even_with_a_live_ticket(self):
        # A pure move (origin only, roster unchanged) repositions the existing
        # seats in place, so the live ticket stays attached to its seat rather
        # than being orphaned -- this is the case the old regenerate-always
        # path refused outright.
        self._login_as("manager")
        seat = self.seats[0]
        seat_pk = seat.pk
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
        ticket = Ticket.objects.create(
            organization=self.org, order=order, performance=performance, seat=seat
        )

        resp = self._post_json(
            self._save_url(),
            {"sections": {str(self.section.pk): self._params_payload(origin_x=300.0)}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["saved"], [self.section.pk])
        self.section.refresh_from_db()
        self.assertEqual(self.section.origin_x, 300.0)
        # Same seat rows (pks preserved) -- shifted, not regenerated.
        self.assertEqual(self.section.seats.count(), 9)
        ticket.refresh_from_db()
        self.assertEqual(ticket.seat_id, seat_pk)
        self.assertTrue(all(s.x >= 300.0 for s in self.section.seats.all()))

    def test_save_one_bad_section_does_not_block_the_others_in_the_same_batch(self):
        self._login_as("manager")
        ok_section = Section.objects.create(
            organization=self.org, chart=self.chart, name="Balcony", rows=2, seats_per_row=2
        )
        generate_seats(ok_section, [2, 2])
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

        # self.section has a live ticket AND its payload drops a row
        # (rows=2, from 3) -- a roster change, so it can't reposition in
        # place and falls back to a regenerate that the guardrail refuses.
        # ok_section still saves in the same batch.
        resp = self._post_json(
            self._save_url(),
            {
                "sections": {
                    str(self.section.pk): self._params_payload(rows=2, origin_x=1.0),
                    str(ok_section.pk): self._params_payload(section=ok_section, origin_x=77.0),
                }
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn(str(self.section.pk), data["errors"])
        self.assertEqual(data["saved"], [ok_section.pk])
        ok_section.refresh_from_db()
        self.assertEqual(ok_section.origin_x, 77.0)

    # -- save endpoint: tenant isolation -------------------------------

    def test_save_cannot_touch_another_orgs_section(self):
        other_chart = SeatingChart.objects.create(
            organization=self.other_org, venue=self.other_venue, name="Other"
        )
        other_section = Section.objects.create(
            organization=self.other_org, chart=other_chart, name="Other Orchestra"
        )
        generate_seats(other_section, [1])
        original_origin_x = other_section.origin_x

        self._login_as("manager")  # manager of self.org, NOT other_org
        resp = self._post_json(
            self._save_url(),
            {"sections": {str(other_section.pk): self._params_payload(section=other_section, origin_x=500.0)}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["saved"], [])  # nothing mutated -- silently excluded
        other_section.refresh_from_db()
        self.assertEqual(other_section.origin_x, original_origin_x)

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
        generate_seats(other_section, [1])

        self._login_as("manager")
        resp = self._post_json(
            self._save_url(other_chart),
            {"sections": {str(other_section.pk): self._params_payload(section=other_section, origin_x=1.0)}},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)
        other_section.refresh_from_db()
        self.assertNotEqual(other_section.origin_x, 1.0)

    def test_save_rejects_bad_payload_shapes(self):
        self._login_as("manager")
        for bad_payload in [{}, {"sections": []}, {"sections": {}}, {"sections": {"abc": {}}}]:
            resp = self._post_json(self._save_url(), bad_payload, HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, 400, f"payload {bad_payload!r}")


class PricingZoneEditorTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """Phase C (docs/SEATING.md "C"): the per-performance zone editor page
    and its apply/remove-seats/delete/clone JSON endpoints -- manager-gated,
    org-scoped exactly like the Phase B chart editor, plus the zone-specific
    invariants (a seat in at most one zone per performance, template edits
    don't retroactively change an applied zone, clone creates independent
    instances)."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.event, self.performance, self.section, self.seats = self.build_reserved_event(
            self.org, self.venue, n_seats=4
        )
        PriceTier.objects.create(
            organization=self.org, section=self.section, name="Orchestra", amount=Decimal("65.00")
        )
        self.template = ZoneTemplate.objects.create(
            organization=self.org, name="Premium", color="#c1121f"
        )
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

    def _zones_url(self):
        return f"/dashboard/performances/{self.performance.pk}/pricing-zones/"

    def _apply_url(self):
        return f"/dashboard/performances/{self.performance.pk}/pricing-zones/apply/"

    def _remove_url(self):
        return f"/dashboard/performances/{self.performance.pk}/pricing-zones/remove-seats/"

    def _delete_url(self, zone_pk):
        return f"/dashboard/performances/{self.performance.pk}/pricing-zones/{zone_pk}/delete/"

    def _clone_url(self):
        return f"/dashboard/performances/{self.performance.pk}/pricing-zones/clone/"

    # -- editor page: role gate + renders --------------------------------

    def test_editor_page_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self.client.get(self._zones_url(), HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> zone editor")

    def test_editor_anonymous_redirected_to_login(self):
        resp = self.client.get(self._zones_url(), HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_editor_cross_org_performance_404s(self):
        self._login_as("manager")
        other_event, other_performance, _, _ = self.build_reserved_event(self.other_org, self.other_venue)
        resp = self.client.get(
            f"/dashboard/performances/{other_performance.pk}/pricing-zones/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)

    def test_editor_redirects_for_ga_performance(self):
        self._login_as("manager")
        ga_event, ga_performance, _ = self.build_ga_event(self.org, self.venue, slug="ga-show-2")
        resp = self.client.get(
            f"/dashboard/performances/{ga_performance.pk}/pricing-zones/", HTTP_HOST=host_for("roxy")
        )
        self.assertRedirects(
            resp, f"/dashboard/events/{ga_event.pk}/", fetch_redirect_response=False
        )

    def test_editor_renders_every_seat_and_the_existing_template(self):
        self._login_as("manager")
        resp = self.client.get(self._zones_url(), HTTP_HOST=host_for("roxy"))
        content = resp.content.decode()
        self.assertEqual(content.count("zone-editor-seat"), 4)
        for seat in self.seats:
            self.assertIn(f'data-seat-id="{seat.pk}"', content)
        self.assertIn("Premium", content)

    # -- apply: role gate ---------------------------------------------------

    def test_apply_manager_and_above_only(self):
        payload = {"seat_ids": [self.seats[0].pk], "amount": "95.00", "template_id": self.template.pk}
        for role, expected in [("box_office", 403), ("scanner", 403), ("owner", 200), ("manager", 200)]:
            self._login_as(role)
            resp = self._post_json(self._apply_url(), payload, HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> apply zone")

    def test_apply_anonymous_redirected_to_login(self):
        resp = self._post_json(
            self._apply_url(),
            {"seat_ids": [self.seats[0].pk], "amount": "95.00", "template_id": self.template.pk},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    # -- apply: behavior ------------------------------------------------------

    def test_apply_with_existing_template_creates_zone_and_assigns_seats(self):
        self._login_as("manager")
        resp = self._post_json(
            self._apply_url(),
            {
                "seat_ids": [self.seats[0].pk, self.seats[1].pk],
                "amount": "95.00",
                "template_id": self.template.pk,
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["zones"]), 1)
        zone = data["zones"][0]
        self.assertEqual(zone["name"], "Premium")
        self.assertEqual(zone["amount"], "95.00")
        self.assertCountEqual(zone["seat_ids"], [self.seats[0].pk, self.seats[1].pk])

    def test_apply_with_new_name_and_color_creates_a_reusable_template(self):
        self._login_as("manager")
        resp = self._post_json(
            self._apply_url(),
            {
                "seat_ids": [self.seats[0].pk],
                "amount": "40.00",
                "name": "Standard",
                "color": "#1d4ed8",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(ZoneTemplate.objects.filter(organization=self.org, name="Standard").exists())
        zone = PricingZone.objects.get(performance=self.performance, name="Standard")
        self.assertEqual(zone.amount, Decimal("40.00"))

    def test_a_seat_can_only_be_in_one_zone_per_performance(self):
        self._login_as("manager")
        other_template = ZoneTemplate.objects.create(organization=self.org, name="Standard", color="#1d4ed8")
        self._post_json(
            self._apply_url(),
            {"seat_ids": [self.seats[0].pk], "amount": "95.00", "template_id": self.template.pk},
            HTTP_HOST=host_for("roxy"),
        )
        resp = self._post_json(
            self._apply_url(),
            {"seat_ids": [self.seats[0].pk], "amount": "40.00", "template_id": other_template.pk},
            HTTP_HOST=host_for("roxy"),
        )
        data = resp.json()
        zones_by_name = {z["name"]: z for z in data["zones"]}
        self.assertNotIn(self.seats[0].pk, zones_by_name["Premium"]["seat_ids"])
        self.assertIn(self.seats[0].pk, zones_by_name["Standard"]["seat_ids"])

    def test_apply_rejects_seats_from_another_org(self):
        self._login_as("manager")
        other_event, other_performance, other_section, other_seats = self.build_reserved_event(
            self.other_org, self.other_venue
        )
        resp = self._post_json(
            self._apply_url(),
            {"seat_ids": [other_seats[0].pk], "amount": "95.00", "template_id": self.template.pk},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PricingZone.objects.filter(performance=self.performance).exists())

    def test_apply_cross_org_performance_404s(self):
        self._login_as("manager")
        other_event, other_performance, other_section, other_seats = self.build_reserved_event(
            self.other_org, self.other_venue
        )
        resp = self._post_json(
            f"/dashboard/performances/{other_performance.pk}/pricing-zones/apply/",
            {"seat_ids": [other_seats[0].pk], "amount": "95.00", "template_id": self.template.pk},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_apply_invalid_amount_rejected(self):
        self._login_as("manager")
        resp = self._post_json(
            self._apply_url(),
            {"seat_ids": [self.seats[0].pk], "amount": "not-a-number", "template_id": self.template.pk},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 400)

    # -- remove seats / delete ------------------------------------------------

    def test_remove_selected_seats_from_zone(self):
        self._login_as("manager")
        self._post_json(
            self._apply_url(),
            {
                "seat_ids": [self.seats[0].pk, self.seats[1].pk],
                "amount": "95.00",
                "template_id": self.template.pk,
            },
            HTTP_HOST=host_for("roxy"),
        )
        zone = PricingZone.objects.get(performance=self.performance)
        resp = self._post_json(
            self._remove_url(),
            {"zone_id": zone.pk, "seat_ids": [self.seats[0].pk]},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        zone.refresh_from_db()
        self.assertCountEqual(zone.seats.values_list("pk", flat=True), [self.seats[1].pk])

    def test_remove_seats_cross_org_zone_404s(self):
        self._login_as("manager")
        other_event, other_performance, other_section, other_seats = self.build_reserved_event(
            self.other_org, self.other_venue
        )
        other_template = ZoneTemplate.objects.create(
            organization=self.other_org, name="Premium", color="#c1121f"
        )
        other_zone = PricingZone.objects.create(
            organization=self.other_org,
            performance=other_performance,
            template=other_template,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        other_zone.seats.add(other_seats[0], through_defaults={"organization": self.other_org})

        resp = self._post_json(
            self._remove_url(),
            {"zone_id": other_zone.pk, "seat_ids": [other_seats[0].pk]},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)
        other_zone.refresh_from_db()
        self.assertEqual(other_zone.seats.count(), 1)  # untouched

    def test_delete_zone(self):
        self._login_as("manager")
        self._post_json(
            self._apply_url(),
            {"seat_ids": [self.seats[0].pk], "amount": "95.00", "template_id": self.template.pk},
            HTTP_HOST=host_for("roxy"),
        )
        zone = PricingZone.objects.get(performance=self.performance)
        resp = self._post_json(self._delete_url(zone.pk), {}, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PricingZone.objects.filter(pk=zone.pk).exists())

    def test_delete_zone_cross_org_404s_and_does_not_mutate(self):
        self._login_as("manager")
        other_event, other_performance, other_section, other_seats = self.build_reserved_event(
            self.other_org, self.other_venue
        )
        other_template = ZoneTemplate.objects.create(
            organization=self.other_org, name="Premium", color="#c1121f"
        )
        other_zone = PricingZone.objects.create(
            organization=self.other_org,
            performance=other_performance,
            template=other_template,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        resp = self._post_json(
            f"/dashboard/performances/{self.performance.pk}/pricing-zones/{other_zone.pk}/delete/",
            {},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PricingZone.objects.filter(pk=other_zone.pk).exists())

    def test_delete_manager_and_above_only(self):
        zone = PricingZone.objects.create(
            organization=self.org,
            performance=self.performance,
            template=self.template,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        for role, expected in [("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self._post_json(self._delete_url(zone.pk), {}, HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> delete zone")
        self._login_as("manager")
        resp = self._post_json(self._delete_url(zone.pk), {}, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)

    # -- clone from another performance ----------------------------------------

    def test_clone_zones_from_another_performance(self):
        self._login_as("manager")
        # A second Venue in the same org -- build_reserved_event always names
        # its chart "Standard", which would collide with self.performance's
        # own chart if reused on the same Venue.
        source_venue = Venue.objects.create(organization=self.org, name="Second Stage")
        source_event, source_performance, source_section, source_seats = self.build_reserved_event(
            self.org, source_venue, slug="source-show", n_seats=2
        )
        source_zone = PricingZone.objects.create(
            organization=self.org,
            performance=source_performance,
            template=self.template,
            name="Premium",
            color="#c1121f",
            amount=Decimal("95.00"),
        )
        # Cloning only carries over seats that exist on the TARGET
        # performance's own chart (events.zones.clone_zones_from_performance)
        # -- put one of self.performance's own seats in source_zone so it's
        # guaranteed to transfer regardless of source_performance's chart.
        source_zone.seats.add(self.seats[0], through_defaults={"organization": self.org})

        resp = self._post_json(
            self._clone_url(),
            {"source_performance_id": source_performance.pk},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["zones"]), 1)
        cloned = PricingZone.objects.get(performance=self.performance)
        self.assertNotEqual(cloned.pk, source_zone.pk)
        self.assertEqual(cloned.amount, Decimal("95.00"))
        self.assertIn(self.seats[0].pk, cloned.seats.values_list("pk", flat=True))

        # Editing the clone never mutates the source.
        cloned.amount = Decimal("1.00")
        cloned.save(update_fields=["amount"])
        source_zone.refresh_from_db()
        self.assertEqual(source_zone.amount, Decimal("95.00"))

    def test_clone_cross_org_source_scoped_to_requesting_org(self):
        self._login_as("manager")
        other_event, other_performance, other_section, other_seats = self.build_reserved_event(
            self.other_org, self.other_venue
        )
        resp = self._post_json(
            self._clone_url(),
            {"source_performance_id": other_performance.pk},
            HTTP_HOST=host_for("roxy"),
        )
        # source_performance is looked up scoped to request.organization --
        # another org's performance id 404s, never leaking its zones.
        self.assertEqual(resp.status_code, 404)

    def test_clone_manager_and_above_only(self):
        source_venue = Venue.objects.create(organization=self.org, name="Second Stage")
        source_event, source_performance, _, _ = self.build_reserved_event(
            self.org, source_venue, slug="source-show-2"
        )
        for role, expected in [("box_office", 403), ("scanner", 403), ("manager", 200)]:
            self._login_as(role)
            resp = self._post_json(
                self._clone_url(),
                {"source_performance_id": source_performance.pk},
                HTTP_HOST=host_for("roxy"),
            )
            self.assertEqual(resp.status_code, expected, f"{role} -> clone zones")


class PricingZoneExportTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """Phase D (docs/SEATING.md "D"): the PNG/PDF export view --
    manager-gated and org-scoped exactly like the rest of the zone editor
    (PricingZoneEditorTests above), plus the download-specific bits
    (content-type, `Content-Disposition: attachment`, format/size/labels/
    legend query params). The renderer itself (events.zone_export) has its
    own thorough tests in events/test_zone_export.py -- these only check
    the HTTP layer wires it up correctly."""

    def setUp(self):
        from events.zones import apply_zone

        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.event, self.performance, self.section, self.seats = self.build_reserved_event(
            self.org, self.venue, n_seats=4
        )
        for i, seat in enumerate(self.seats):
            seat.x, seat.y = float(i), 0.0
            seat.save(update_fields=["x", "y"])
        PriceTier.objects.create(
            organization=self.org, section=self.section, name="Orchestra", amount=Decimal("65.00")
        )
        self.template = ZoneTemplate.objects.create(
            organization=self.org, name="Premium", color="#c1121f"
        )
        apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[self.seats[0].pk],
            amount=Decimal("95.00"),
            template=self.template,
        )
        self.roles = {
            "owner": self.make_staff(self.org, Membership.Role.OWNER)[0],
            "manager": self.make_staff(self.org, Membership.Role.MANAGER)[0],
            "box_office": self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0],
            "scanner": self.make_staff(self.org, Membership.Role.SCANNER)[0],
        }

    def _login_as(self, role):
        self.client.logout()
        self.client.force_login(self.roles[role])

    def _export_url(self, pk=None):
        return f"/dashboard/performances/{pk or self.performance.pk}/pricing-zones/export/"

    def test_export_manager_and_above_only(self):
        for role, expected in [("owner", 200), ("manager", 200), ("box_office", 403), ("scanner", 403)]:
            self._login_as(role)
            resp = self.client.get(self._export_url(), HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, f"{role} -> zone export")

    def test_export_anonymous_redirected_to_login(self):
        resp = self.client.get(self._export_url(), HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_export_cross_org_performance_404s(self):
        self._login_as("manager")
        other_event, other_performance, _, _ = self.build_reserved_event(self.other_org, self.other_venue)
        resp = self.client.get(self._export_url(other_performance.pk), HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 404)

    def test_default_download_is_png_with_attachment_header(self):
        self._login_as("manager")
        resp = self.client.get(self._export_url(), HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertIn("attachment;", resp["Content-Disposition"])
        self.assertIn(".png", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"\x89PNG"))

    def test_format_pdf_query_param(self):
        self._login_as("manager")
        resp = self.client.get(self._export_url() + "?format=pdf", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn(".pdf", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"%PDF-"))

    def test_size_and_toggle_query_params_accepted(self):
        self._login_as("manager")
        resp = self.client.get(
            self._export_url() + "?format=pdf&size=legal&labels=0&legend=0", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.startswith(b"%PDF-"))

    def test_ga_performance_still_renders_a_mostly_blank_sheet(self):
        # Unlike the JSON zone-editor endpoints, export doesn't reject a GA
        # performance outright -- it just has no seats to draw (see
        # performance_zone_export's docstring).
        self._login_as("manager")
        ga_event, ga_performance, _ = self.build_ga_event(self.org, self.venue, slug="ga-show-export")
        resp = self.client.get(self._export_url(ga_performance.pk), HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")

    def test_zoneless_reserved_performance_renders(self):
        self._login_as("manager")
        second_venue = Venue.objects.create(organization=self.org, name="Second Stage")
        event2, performance2, section2, seats2 = self.build_reserved_event(
            self.org, second_venue, slug="zoneless-show"
        )
        resp = self.client.get(self._export_url(performance2.pk), HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.startswith(b"\x89PNG"))


class OrderActionsTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """The staff order actions (BO-4): resend tickets, cancel/void, refund.
    Gated to box_office+, org-scoped by token, and correct about freeing
    inventory + recording the reversal."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.event, self.performance, self.tier = self.build_ga_event(
            self.org, self.venue, capacity=10, sold=2
        )
        self.box_office, self.pw = self.make_staff(self.org, Membership.Role.BOX_OFFICE)
        self.client.force_login(self.box_office)

    def _order(self, provider="stripe", provider_ref="pi_test_123"):
        order = self.make_paid_order(self.org, self.performance, "40.00", n_tickets=2)
        Payment.objects.create(
            organization=self.org, order=order, provider=provider, amount=Decimal("40.00"),
            status="succeeded", provider_ref=provider_ref,
        )
        return order

    def _post(self, order, action):
        return self.client.post(
            f"/dashboard/orders/{order.token}/{action}/", HTTP_HOST=host_for("roxy")
        )

    def test_resend_sends_email(self):
        order = self._order()
        resp = self._post(order, "resend")
        self.assertRedirects(resp, f"/dashboard/orders/{order.token}/", fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])

    def test_cancel_voids_tickets_and_frees_ga_inventory(self):
        order = self._order()
        self.assertEqual(self.performance.ga_allocation.sold, 2)
        self._post(order, "cancel")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(order.tickets.exclude(status=Ticket.Status.VOID).count(), 0)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 0)  # 2 freed

    def test_refund_stripe_order_calls_stripe_and_marks_refunded(self):
        order = self._order(provider="stripe", provider_ref="pi_test_123")
        with patch("payments.services.stripe.Refund.create") as mock_refund:
            mock_refund.return_value = type("R", (), {"id": "re_test_1"})()
            self._post(order, "refund")
            mock_refund.assert_called_once()
            _, kwargs = mock_refund.call_args
            self.assertEqual(kwargs["payment_intent"], "pi_test_123")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)
        self.assertEqual(order.tickets.exclude(status=Ticket.Status.VOID).count(), 0)
        self.assertTrue(order.payments.filter(status="refunded", provider_ref="re_test_1").exists())
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 0)

    def test_refund_stub_order_needs_no_stripe_call(self):
        order = self._order(provider="stub", provider_ref="stub-abc")
        with patch("payments.services.stripe.Refund.create") as mock_refund:
            self._post(order, "refund")
            mock_refund.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)

    def test_refund_is_idempotent(self):
        order = self._order(provider="stub", provider_ref="stub-abc")
        self._post(order, "refund")
        with patch("payments.services.stripe.Refund.create") as mock_refund:
            self._post(order, "refund")  # second time: no-op
            mock_refund.assert_not_called()
        self.assertEqual(order.payments.filter(status="refunded").count(), 1)

    def test_scanner_cannot_act_on_orders(self):
        scanner, _ = self.make_staff(self.org, Membership.Role.SCANNER, email="s@roxy.example.com")
        self.client.force_login(scanner)
        order = self._order()
        resp = self._post(order, "cancel")
        self.assertEqual(resp.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_cannot_act_on_another_orgs_order(self):
        org_b, venue_b = self.build_org("other")
        _, perf_b, _ = self.build_ga_event(org_b, venue_b, slug="b-show")
        order_b = self.make_paid_order(org_b, perf_b, "40.00", n_tickets=1)
        resp = self._post(order_b, "cancel")  # box_office is logged into roxy
        self.assertEqual(resp.status_code, 404)
        order_b.refresh_from_db()
        self.assertEqual(order_b.status, Order.Status.PAID)
