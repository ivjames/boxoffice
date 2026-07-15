"""Tests for the dashboard Donations area (dashboard.views.donation_settings
/ donations_report): manager+ role gating, tenant isolation, the settings
form's suggested_amounts validation, the report's totals/date-filter/CSV
export, donation-only orders rendering in the existing order list/detail
pages, and that the overview's per-performance/per-event revenue groupings
exclude donation-only (null-performance) orders while gross revenue still
counts them. Setup style mirrors dashboard/test_promos.py."""

import csv
import io
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from dashboard.tests import DashFixtureMixin
from donations.models import DonationCampaign
from donations.services import get_or_create_general_fund
from orders.models import Order, OrderItem


class DonationSettingsAccessTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.box_office = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.scanner = self.make_staff(self.org, Membership.Role.SCANNER)[0]

    def test_settings_manager_and_above_only(self):
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/donations/settings/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_report_manager_and_above_only(self):
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/donations/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/dashboard/donations/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_settings_visits_dont_create_a_second_campaign(self):
        # get_or_create_general_fund is idempotent (ordered by created_at,
        # pk) -- visiting settings twice must not spawn a second row.
        self.client.force_login(self.manager)
        self.client.get("/dashboard/donations/settings/", HTTP_HOST=host_for("roxy"))
        self.client.get("/dashboard/donations/settings/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(DonationCampaign.objects.filter(organization=self.org).count(), 1)


class DonationSettingsFormTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def _post(self, **overrides):
        data = {
            "is_active": "on",
            "name": "General Fund",
            "suggested_amounts": "10,25,50,100",
            "acknowledgment": "We're a 501(c)(3); no goods or services were provided.",
        }
        data.update(overrides)
        return self.client.post("/dashboard/donations/settings/", data, HTTP_HOST=host_for("roxy"))

    def test_save_updates_the_orgs_campaign(self):
        resp = self._post(
            suggested_amounts="5,15,30", acknowledgment="Thank you for supporting the Roxy."
        )
        self.assertRedirects(
            resp, "/dashboard/donations/settings/", fetch_redirect_response=False
        )
        campaign = DonationCampaign.objects.get(organization=self.org)
        self.assertEqual(campaign.suggested_amounts, "5,15,30")
        self.assertEqual(campaign.acknowledgment, "Thank you for supporting the Roxy.")
        self.assertTrue(campaign.is_active)

    def test_unchecking_is_active_turns_donations_off(self):
        get_or_create_general_fund(self.org)
        self._post(is_active="")
        campaign = DonationCampaign.objects.get(organization=self.org)
        self.assertFalse(campaign.is_active)

    def test_blank_suggested_amounts_rejected(self):
        # An entirely blank field is caught by the model's own required-field
        # validation before clean_suggested_amounts even runs.
        resp = self._post(suggested_amounts="")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This field is required")

    def test_all_junk_suggested_amounts_rejected(self):
        resp = self._post(suggested_amounts=",,,")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter at least one positive amount")

    def test_non_numeric_entry_rejected(self):
        resp = self._post(suggested_amounts="10,abc,25")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn&#x27;t a valid amount")

    def test_negative_entry_rejected(self):
        resp = self._post(suggested_amounts="10,-5,25")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "must be a positive amount")

    def test_editing_only_touches_this_orgs_campaign(self):
        other_campaign = get_or_create_general_fund(self.other_org)
        other_campaign.suggested_amounts = "1,2,3"
        other_campaign.save(update_fields=["suggested_amounts"])

        self._post(suggested_amounts="7,8,9")

        other_campaign.refresh_from_db()
        self.assertEqual(other_campaign.suggested_amounts, "1,2,3")


class DonationReportTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)
        self.campaign = get_or_create_general_fund(self.org)

    def _make_donation_order(self, org, campaign, amount, *, status=Order.Status.PAID, days_ago=0, email="donor@example.com"):
        order = Order.objects.create(
            organization=org,
            performance=None,
            buyer_email=email,
            total=Decimal(amount),
            status=status,
        )
        if days_ago:
            Order.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
            order.refresh_from_db()
        OrderItem.objects.create(
            organization=org,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal(amount),
            donation_campaign=campaign,
        )
        return order

    def test_totals_only_count_paid_donation_orders(self):
        self._make_donation_order(self.org, self.campaign, "20.00")
        self._make_donation_order(self.org, self.campaign, "30.00")
        self._make_donation_order(self.org, self.campaign, "999.00", status=Order.Status.PENDING)

        resp = self.client.get("/dashboard/donations/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["total"], Decimal("50.00"))
        self.assertEqual(len(resp.context["items"]), 2)

    def test_excludes_ticket_only_orders(self):
        from events.models import Event, GAAllocation, Performance, PriceTier

        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org, performance=performance, capacity=10)
        tier = PriceTier.objects.create(
            organization=self.org, performance=performance, name="GA", amount=Decimal("20.00")
        )
        order = Order.objects.create(
            organization=self.org,
            performance=performance,
            buyer_email="ticket@example.com",
            total=Decimal("20.00"),
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            kind=OrderItem.Kind.TICKET,
            price_tier=tier,
            quantity=1,
            unit_amount=Decimal("20.00"),
        )
        self._make_donation_order(self.org, self.campaign, "10.00")

        resp = self.client.get("/dashboard/donations/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["total"], Decimal("10.00"))

    def test_date_filter_narrows_results(self):
        self._make_donation_order(self.org, self.campaign, "20.00", days_ago=10)
        recent = self._make_donation_order(self.org, self.campaign, "30.00", days_ago=0)

        start = (timezone.now() - timedelta(days=1)).date().isoformat()
        resp = self.client.get(
            f"/dashboard/donations/?start={start}", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.context["total"], Decimal("30.00"))
        items = list(resp.context["items"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].order_id, recent.pk)

    def test_tenant_isolation(self):
        other_campaign = get_or_create_general_fund(self.other_org)
        self._make_donation_order(self.other_org, other_campaign, "999.00")
        self._make_donation_order(self.org, self.campaign, "5.00")

        resp = self.client.get("/dashboard/donations/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["total"], Decimal("5.00"))

    def test_csv_export(self):
        order = self._make_donation_order(self.org, self.campaign, "42.00", email="giver@example.com")

        resp = self.client.get("/dashboard/donations/?format=csv", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("attachment", resp["Content-Disposition"])

        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(rows[0], ["Date", "Order token", "Buyer email", "Buyer name", "Campaign", "Amount"])
        data_row = rows[1]
        self.assertEqual(data_row[1], order.token)
        self.assertEqual(data_row[2], "giver@example.com")
        self.assertEqual(data_row[4], self.campaign.name)
        self.assertEqual(data_row[5], "42.00")


class DonationOrderDashboardRenderingTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """Donation-only orders (Order.performance null) must render in the
    existing staff order list/detail pages without 500ing."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.client.force_login(self.owner)
        self.campaign = get_or_create_general_fund(self.org)
        self.order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="donor@example.com",
            buyer_name="Generous Donor",
            total=Decimal("15.00"),
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=self.order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal("15.00"),
            donation_campaign=self.campaign,
        )

    def test_order_list_shows_donation_row(self):
        resp = self.client.get("/dashboard/orders/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Donation")
        self.assertContains(resp, self.order.token)

    def test_order_detail_renders_without_500(self):
        resp = self.client.get(
            f"/dashboard/orders/{self.order.token}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Donation order")
        self.assertContains(resp, self.campaign.name)
        self.assertContains(resp, "$15.00")

    def test_order_detail_items_context_includes_the_donation_item(self):
        resp = self.client.get(
            f"/dashboard/orders/{self.order.token}/", HTTP_HOST=host_for("roxy")
        )
        items = list(resp.context["items"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, OrderItem.Kind.DONATION)


class OverviewDonationRevenueTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    """dashboard.views.overview: gross_revenue includes donations, but the
    per-performance and per-event revenue groupings (a strictly ticketing
    view) exclude null-performance donation orders."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)
        self.campaign = get_or_create_general_fund(self.org)

    def _donation_order(self, amount):
        order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="donor@example.com",
            total=Decimal(amount),
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal(amount),
            donation_campaign=self.campaign,
        )
        return order

    def test_gross_revenue_includes_donations(self):
        _event, performance, _tier = self.build_ga_event(self.org, self.venue)
        self.make_paid_order(self.org, performance, "20.00")
        self._donation_order("30.00")

        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["gross_revenue"], Decimal("50.00"))

    def test_per_event_and_per_performance_rows_exclude_donation_orders(self):
        _event, performance, _tier = self.build_ga_event(self.org, self.venue)
        self.make_paid_order(self.org, performance, "20.00")
        self._donation_order("30.00")

        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))

        # Gross revenue still counts the donation...
        self.assertEqual(resp.context["gross_revenue"], Decimal("50.00"))
        # ...but the per-event table only shows the ticketed event's $20,
        # never a bogus "None" event row for the donation.
        event_rows = resp.context["event_revenue_rows"]
        self.assertEqual(len(event_rows), 1)
        self.assertEqual(event_rows[0]["revenue"], Decimal("20.00"))
        # And the per-performance rows agree.
        perf_rows = {row["performance"].pk: row for row in resp.context["performance_rows"]}
        self.assertEqual(perf_rows[performance.pk]["revenue"], Decimal("20.00"))

    def test_donation_only_org_has_no_event_revenue_rows_but_has_gross(self):
        self._donation_order("40.00")
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["gross_revenue"], Decimal("40.00"))
        self.assertEqual(resp.context["event_revenue_rows"], [])
