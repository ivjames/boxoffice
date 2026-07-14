"""Tests for the dashboard overview's "Getting started" checklist
(dashboard.views.overview, Phase 5 of docs/ROADMAP.md -- support posture).
Manager+ only (same gate as show_revenue): computes 10 setup steps, shows a
progress card that links out to the undone ones, and disappears once every
step is done so an established theater stops being nagged. Setup style
mirrors dashboard/tests.py's OverviewReportTests / OverviewRevenueGateTests.
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from dashboard.tests import DashFixtureMixin
from events.models import Event, PriceTier
from orders.models import Order
from venues.models import SeatingChart, Venue
from venues.tests import make_org


class OnboardingChecklistTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        # A genuinely blank org -- make_org() alone, no venue -- so the
        # checklist starts with nothing done (build_org() from
        # DashFixtureMixin pre-creates a venue, which would skew done_count).
        self.org = make_org("roxy")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_brand_new_org_shows_checklist_mostly_undone(self):
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Getting started")
        self.assertTrue(resp.context["show_onboarding"])
        self.assertEqual(resp.context["onboarding_total"], 10)
        self.assertFalse(resp.context["onboarding_all_done"])
        steps_by_key = {s["key"]: s for s in resp.context["onboarding_steps"]}
        # "Invite a teammate" only excludes OWNER (per the spec query), so
        # the logged-in manager's own membership already satisfies it --
        # a known quirk of the literal query, not a bug we're papering over.
        self.assertTrue(steps_by_key["teammate"]["done"])
        self.assertEqual(resp.context["onboarding_done_count"], 1)
        for key, step in steps_by_key.items():
            if key != "teammate":
                self.assertFalse(step["done"], key)

    def test_undone_steps_with_a_url_render_as_links(self):
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, f'href="{"/dashboard/venues/"}"')
        self.assertContains(resp, f'href="{"/dashboard/events/"}"')
        self.assertContains(resp, f'href="{"/dashboard/team/"}"')

    def test_steps_without_a_destination_render_as_plain_text_not_links(self):
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        steps_by_key = {s["key"]: s for s in resp.context["onboarding_steps"]}
        self.assertIsNone(steps_by_key["stripe"]["url"])
        self.assertIsNone(steps_by_key["branding"]["url"])
        self.assertIsNone(steps_by_key["first_sale"]["url"])

    def test_adding_a_venue_flips_that_step_done_and_increments_count(self):
        Venue.objects.create(organization=self.org, name="Main Stage")
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        steps_by_key = {s["key"]: s for s in resp.context["onboarding_steps"]}
        self.assertTrue(steps_by_key["venue"]["done"])
        # +1 for venue on top of the baseline "teammate" step (see the
        # brand-new-org test above for why that one's already done).
        self.assertEqual(resp.context["onboarding_done_count"], 2)

    def test_publishing_event_and_pricing_flips_those_steps(self):
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        event, performance, tier = self.build_ga_event(self.org, venue)
        # build_ga_event already publishes the event + performance and adds
        # a PriceTier, so venue/event/publish_event/price_tier/
        # performance_on_sale should all be done now.
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        steps_by_key = {s["key"]: s for s in resp.context["onboarding_steps"]}
        self.assertTrue(steps_by_key["venue"]["done"])
        self.assertTrue(steps_by_key["event"]["done"])
        self.assertTrue(steps_by_key["publish_event"]["done"])
        self.assertTrue(steps_by_key["price_tier"]["done"])
        self.assertTrue(steps_by_key["performance_on_sale"]["done"])
        # 5 from this fixture + the baseline "teammate" step.
        self.assertEqual(resp.context["onboarding_done_count"], 6)

    def test_inviting_a_teammate_flips_that_step(self):
        self.make_staff(self.org, Membership.Role.BOX_OFFICE, email="bo@roxy.example.com")
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        steps_by_key = {s["key"]: s for s in resp.context["onboarding_steps"]}
        self.assertTrue(steps_by_key["teammate"]["done"])

    def test_a_paid_order_flips_first_sale_done(self):
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        _event, performance, _tier = self.build_ga_event(self.org, venue)
        self.make_paid_order(self.org, performance, "20.00")
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        steps_by_key = {s["key"]: s for s in resp.context["onboarding_steps"]}
        self.assertTrue(steps_by_key["first_sale"]["done"])

    def test_all_steps_done_hides_the_card(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")
        _event, performance, _tier = self.build_ga_event(self.org, venue)
        self.make_staff(self.org, Membership.Role.BOX_OFFICE, email="bo@roxy.example.com")
        self.make_paid_order(self.org, performance, "20.00")
        self.org.stripe_charges_enabled = True
        # Tiny 1x1 GIF -- just needs to be a truthy ImageField value.
        gif = (
            b"GIF87a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff,"
            b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        )
        self.org.logo = SimpleUploadedFile("logo.gif", gif, content_type="image/gif")
        self.org.save(update_fields=["stripe_charges_enabled", "logo"])

        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertTrue(resp.context["onboarding_all_done"])
        self.assertFalse(resp.context["show_onboarding"])
        self.assertEqual(
            resp.context["onboarding_done_count"], resp.context["onboarding_total"]
        )
        self.assertNotContains(resp, "Getting started")

    def test_box_office_never_sees_the_card_even_with_nothing_set_up(self):
        # Onboarding is gated on can_manage_events, same as show_revenue --
        # box office runs the door, it doesn't need setup nagging.
        user = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.client.logout()
        self.client.force_login(user)
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["show_onboarding"])
        self.assertNotContains(resp, "Getting started")

    def test_scanner_is_redirected_to_scan_not_shown_the_overview_at_all(self):
        user = self.make_staff(self.org, Membership.Role.SCANNER)[0]
        self.client.logout()
        self.client.force_login(user)
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/scan/")

    def test_owner_of_zero_data_org_does_not_crash(self):
        owner = self.make_staff(self.org, Membership.Role.OWNER, email="owner2@roxy.example.com")[0]
        self.client.logout()
        self.client.force_login(owner)
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)

    def test_zone_pricing_counts_as_prices_set(self):
        # A reserved-seat show priced entirely with PricingZones (no PriceTier
        # rows) still has prices set -- the step must read done off zones too,
        # else a zone-only theater is nagged forever.
        from events.models import PricingZone

        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        _event, performance, tier = self.build_ga_event(self.org, venue)
        tier.delete()  # leave zero PriceTier rows for the org
        PricingZone.objects.create(
            organization=self.org,
            performance=performance,
            name="Orchestra",
            color="#334455",
            amount=Decimal("42.00"),
        )
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        steps_by_key = {s["key"]: s for s in resp.context["onboarding_steps"]}
        self.assertFalse(PriceTier.objects.filter(organization=self.org).exists())
        self.assertTrue(steps_by_key["price_tier"]["done"])

    def test_owner_gets_actionable_stripe_button_manager_does_not(self):
        # The Stripe step is billing-gated (connect_start is @billing_required):
        # an owner (can_manage_billing) gets a POST "Start" button; a plain
        # manager, who can't finish onboarding, sees only text -- no dead button.
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))  # setUp = manager
        self.assertNotContains(resp, 'action="/dashboard/payments/connect/"')

        owner = self.make_staff(self.org, Membership.Role.OWNER, email="owner3@roxy.example.com")[0]
        self.client.logout()
        self.client.force_login(owner)
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, 'action="/dashboard/payments/connect/"')
        # setUp's self.manager membership is still on this org (non-owner),
        # so "teammate" reads done even though there's no other real data --
        # see the brand-new-org test's comment on that query's quirk.
        self.assertEqual(resp.context["onboarding_done_count"], 1)
