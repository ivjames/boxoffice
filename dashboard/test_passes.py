"""Tests for the dashboard Passes area (dashboard.views.PassProduct* / pass_
toggle / pass_report): manager+ role gating, tenant isolation, PassProductForm's
flex/season credit-shape validation, the report's totals/CSV/outstanding-
liability math, order_cancel restoring a redeemed pass's entitlements (the
REQUIRED handoff fix from the foundation agent), and that a pass purchase /
pass redemption order renders on the existing order detail page. Setup style
mirrors dashboard/test_promos.py and dashboard/test_donations.py.
"""

import csv
import io
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from dashboard.tests import DashFixtureMixin
from orders.models import Hold, Order, OrderItem, Ticket
from payments.services import fulfill_hold_with_pass, fulfill_pass_purchase
from passes.models import PassProduct, PassPurchase


class PassFixtureMixin(DashFixtureMixin):
    def make_product(self, org, kind=PassProduct.Kind.FLEX, **kwargs):
        defaults = dict(
            organization=org, name="Flex 3-Pack", kind=kind, price=Decimal("45.00"), is_active=True
        )
        if kind == PassProduct.Kind.FLEX:
            defaults["credit_count"] = kwargs.pop("credit_count", 3)
        defaults.update(kwargs)
        return PassProduct.objects.create(**defaults)

    def buy_pass(self, org, product, email="holder@example.com"):
        return fulfill_pass_purchase(
            org,
            product=product,
            buyer_email=email,
            buyer_name="Holder",
            provider="stub",
            payment_ref="stub-test",
        )

    def redeem(self, org, performance, tier, purchase, session_key="dash-test-session"):
        hold = Hold.objects.create(
            organization=org,
            performance=performance,
            price_tier=tier,
            quantity=1,
            session_key=session_key,
        )
        return fulfill_hold_with_pass(
            hold, purchase, buyer_email=purchase.guest.email, buyer_name=""
        )


class PassAccessTests(StaffFixtureMixin, PassFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.box_office = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.scanner = self.make_staff(self.org, Membership.Role.SCANNER)[0]

    def test_list_manager_and_above_only(self):
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/passes/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_create_get_manager_and_above_only(self):
        for user, expected in [(self.owner, 200), (self.manager, 200), (self.box_office, 403)]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/passes/new/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_report_manager_and_above_only(self):
        for user, expected in [(self.owner, 200), (self.manager, 200), (self.box_office, 403)]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_toggle_manager_and_above_only(self):
        product = self.make_product(self.org)
        for user, expected in [(self.box_office, 403), (self.scanner, 403), (self.manager, 302)]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.post(
                f"/dashboard/passes/{product.pk}/toggle/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, expected, user.email)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/dashboard/passes/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])


class PassCRUDTests(StaffFixtureMixin, PassFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def _create(self, **overrides):
        data = {
            "name": "Flex 3-Pack",
            "kind": PassProduct.Kind.FLEX,
            "price": "45.00",
            "credit_count": "3",
            "valid_from": "",
            "valid_until": "",
            "events": [],
            "is_active": "on",
        }
        data.update(overrides)
        return self.client.post("/dashboard/passes/new/", data, HTTP_HOST=host_for("roxy"))

    def test_create_flex_pass(self):
        resp = self._create()
        product = PassProduct.objects.get(organization=self.org)
        self.assertEqual(product.kind, PassProduct.Kind.FLEX)
        self.assertEqual(product.credit_count, 3)
        self.assertRedirects(resp, "/dashboard/passes/", fetch_redirect_response=False)

    def test_create_is_scoped_to_current_org_even_if_spoofed(self):
        resp = self._create(organization=self.other_org.pk)
        product = PassProduct.objects.get(name="Flex 3-Pack")
        self.assertEqual(product.organization_id, self.org.id)
        self.assertRedirects(resp, "/dashboard/passes/", fetch_redirect_response=False)

    def test_flex_requires_positive_credit_count(self):
        resp = self._create(credit_count="")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "needs a positive credit count")
        self.assertFalse(PassProduct.objects.filter(organization=self.org).exists())

    def test_flex_zero_credit_count_rejected(self):
        resp = self._create(credit_count="0")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "needs a positive credit count")

    def test_season_pass_with_credit_count_rejected(self):
        resp = self._create(
            name="Season Pass", kind=PassProduct.Kind.SEASON, credit_count="3"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "doesn&#x27;t use credits")
        self.assertFalse(PassProduct.objects.filter(organization=self.org).exists())

    def test_season_pass_without_credit_count_created(self):
        resp = self._create(name="Season Pass", kind=PassProduct.Kind.SEASON, credit_count="")
        product = PassProduct.objects.get(organization=self.org)
        self.assertEqual(product.kind, PassProduct.Kind.SEASON)
        self.assertIsNone(product.credit_count)
        self.assertRedirects(resp, "/dashboard/passes/", fetch_redirect_response=False)

    def test_negative_price_rejected(self):
        resp = self._create(price="-5.00")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "can&#x27;t be negative")

    def test_events_field_scoped_to_org(self):
        from events.models import Event

        Event.objects.create(organization=self.org, title="Mine", slug="mine")
        Event.objects.create(organization=self.other_org, title="Theirs", slug="theirs")
        resp = self.client.get("/dashboard/passes/new/", HTTP_HOST=host_for("roxy"))
        form = resp.context["form"]
        titles = {e.title for e in form.fields["events"].queryset}
        self.assertEqual(titles, {"Mine"})

    def test_toggle_deactivates_then_reactivates(self):
        product = self.make_product(self.org)
        self.assertTrue(product.is_active)
        self.client.post(f"/dashboard/passes/{product.pk}/toggle/", HTTP_HOST=host_for("roxy"))
        product.refresh_from_db()
        self.assertFalse(product.is_active)
        self.client.post(f"/dashboard/passes/{product.pk}/toggle/", HTTP_HOST=host_for("roxy"))
        product.refresh_from_db()
        self.assertTrue(product.is_active)

    def test_toggle_cross_org_pk_404s(self):
        other_product = self.make_product(self.other_org, name="Theirs")
        resp = self.client.post(
            f"/dashboard/passes/{other_product.pk}/toggle/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)
        other_product.refresh_from_db()
        self.assertTrue(other_product.is_active)

    def test_update_cross_org_pk_404s(self):
        other_product = self.make_product(self.other_org, name="Theirs")
        resp = self.client.get(
            f"/dashboard/passes/{other_product.pk}/edit/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)

    def test_list_shows_only_this_orgs_products(self):
        self.make_product(self.org, name="Mine")
        self.make_product(self.other_org, name="Theirs")
        resp = self.client.get("/dashboard/passes/", HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, "Mine")
        self.assertNotContains(resp, "Theirs")


class PassReportTests(StaffFixtureMixin, PassFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_totals_only_count_paid_pass_orders(self):
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=2)
        self.buy_pass(self.org, product, "a@example.com")
        self.buy_pass(self.org, product, "b@example.com")

        resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["total"], Decimal("60.00"))
        self.assertEqual(len(resp.context["items"]), 2)

    def test_tenant_isolation(self):
        product = self.make_product(self.org, price=Decimal("20.00"))
        self.buy_pass(self.org, product)
        other_product = self.make_product(self.other_org, price=Decimal("999.00"))
        self.buy_pass(self.other_org, other_product)

        resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["total"], Decimal("20.00"))

    def test_csv_export(self):
        product = self.make_product(self.org, price=Decimal("42.00"), credit_count=2)
        order = self.buy_pass(self.org, product, "giver@example.com")

        resp = self.client.get("/dashboard/passes/report/?format=csv", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(rows[0], ["Date", "Order token", "Buyer email", "Product", "Amount"])
        data_row = rows[1]
        self.assertEqual(data_row[1], order.token)
        self.assertEqual(data_row[2], "giver@example.com")
        self.assertEqual(data_row[3], product.name)
        self.assertEqual(data_row[4], "42.00")

    def test_flex_liability_math(self):
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=3)
        self.buy_pass(self.org, product, "a@example.com")  # 3 credits, untouched
        self.buy_pass(self.org, product, "b@example.com")  # 3 credits, untouched

        resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
        # 3 + 3 = 6 credits outstanding, at $10/credit ($30 / 3) = $60 total.
        self.assertEqual(resp.context["flex_credits_outstanding"], 6)
        self.assertEqual(resp.context["flex_value_outstanding"], Decimal("60.00"))

    def test_flex_liability_shrinks_after_redemption(self):
        _event, performance, tier = self.build_ga_event(self.org, self.venue, capacity=10)
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=3)
        self.buy_pass(self.org, product, "a@example.com")
        purchase = PassPurchase.objects.get(product=product)
        self.redeem(self.org, performance, tier, purchase)

        resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["flex_credits_outstanding"], 2)
        self.assertEqual(resp.context["flex_value_outstanding"], Decimal("20.00"))

    def test_season_admissions_outstanding(self):
        from events.models import Event

        event_a = Event.objects.create(organization=self.org, title="A", slug="a")
        event_b = Event.objects.create(organization=self.org, title="B", slug="b")
        product = self.make_product(
            self.org, kind=PassProduct.Kind.SEASON, price=Decimal("100.00")
        )
        product.events.set([event_a, event_b])
        self.buy_pass(self.org, product, "season@example.com")

        resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["season_admissions_outstanding"], 2)
        self.assertEqual(resp.context["unbounded_season_count"], 0)

    def test_all_access_season_pass_counted_as_unbounded(self):
        product = self.make_product(
            self.org, kind=PassProduct.Kind.SEASON, price=Decimal("100.00")
        )  # no events -> all-access, unbounded
        self.buy_pass(self.org, product, "season@example.com")

        resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["season_admissions_outstanding"], 0)
        self.assertEqual(resp.context["unbounded_season_count"], 1)

    def test_refunded_purchase_excluded_from_liability(self):
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=3)
        self.buy_pass(self.org, product, "a@example.com")
        purchase = PassPurchase.objects.get(product=product)
        purchase.status = PassPurchase.Status.REFUNDED
        purchase.save(update_fields=["status"])

        resp = self.client.get("/dashboard/passes/report/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.context["flex_credits_outstanding"], 0)


class OrderCancelRestoresPassCreditsTests(StaffFixtureMixin, PassFixtureMixin, TestCase):
    """The REQUIRED handoff fix: dashboard.views.order_cancel must call
    passes.services.restore_redemptions_for_order after void_order, so
    cancelling a pass-REDEMPTION order gives the holder their entitlement
    back (season event slot freed / flex credit restored) instead of
    silently eating it."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.client.force_login(self.owner)
        self.event, self.performance, self.tier = self.build_ga_event(
            self.org, self.venue, capacity=10
        )

    def test_cancel_flex_redemption_order_restores_credit(self):
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=3)
        self.buy_pass(self.org, product, "holder@example.com")
        purchase = PassPurchase.objects.get(product=product)
        redemption_order = self.redeem(self.org, self.performance, self.tier, purchase)

        purchase.refresh_from_db()
        self.assertEqual(purchase.credits_remaining, 2)
        self.assertEqual(Ticket.objects.filter(order=redemption_order).count(), 1)

        resp = self.client.post(
            f"/dashboard/orders/{redemption_order.token}/cancel/", HTTP_HOST=host_for("roxy")
        )
        self.assertRedirects(
            resp,
            f"/dashboard/orders/{redemption_order.token}/",
            fetch_redirect_response=False,
        )

        redemption_order.refresh_from_db()
        self.assertEqual(redemption_order.status, Order.Status.CANCELLED)
        purchase.refresh_from_db()
        # The credit is BACK -- this is the handoff fix under test.
        self.assertEqual(purchase.credits_remaining, 3)
        self.assertEqual(purchase.status, PassPurchase.Status.ACTIVE)
        self.assertEqual(redemption_order.pass_redemptions.count(), 0)

    def test_cancel_exhausted_flex_pass_flips_back_to_active(self):
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=1)
        self.buy_pass(self.org, product, "holder@example.com")
        purchase = PassPurchase.objects.get(product=product)
        redemption_order = self.redeem(self.org, self.performance, self.tier, purchase)

        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PassPurchase.Status.EXHAUSTED)

        self.client.post(
            f"/dashboard/orders/{redemption_order.token}/cancel/", HTTP_HOST=host_for("roxy")
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PassPurchase.Status.ACTIVE)
        self.assertEqual(purchase.credits_remaining, 1)

    def test_cancel_season_redemption_frees_the_event_slot(self):
        product = self.make_product(
            self.org, kind=PassProduct.Kind.SEASON, price=Decimal("100.00")
        )
        product.events.set([self.event])
        self.buy_pass(self.org, product, "season@example.com")
        purchase = PassPurchase.objects.get(product=product)
        redemption_order = self.redeem(self.org, self.performance, self.tier, purchase)

        self.assertEqual(purchase.redemptions.count(), 1)
        self.client.post(
            f"/dashboard/orders/{redemption_order.token}/cancel/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(purchase.redemptions.count(), 0)

    def test_cancel_ordinary_ticket_order_unaffected(self):
        # Sanity: the fix must be a no-op for a normal (non-pass) order --
        # restore_redemptions_for_order returns 0 and doesn't error.
        order = self.make_paid_order(self.org, self.performance, "20.00", n_tickets=1)
        resp = self.client.post(
            f"/dashboard/orders/{order.token}/cancel/", HTTP_HOST=host_for("roxy")
        )
        self.assertRedirects(
            resp, f"/dashboard/orders/{order.token}/", fetch_redirect_response=False
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)

    def test_already_cancelled_order_is_a_noop(self):
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=3)
        self.buy_pass(self.org, product, "holder@example.com")
        purchase = PassPurchase.objects.get(product=product)
        redemption_order = self.redeem(self.org, self.performance, self.tier, purchase)
        redemption_order.status = Order.Status.CANCELLED
        redemption_order.save(update_fields=["status"])

        self.client.post(
            f"/dashboard/orders/{redemption_order.token}/cancel/", HTTP_HOST=host_for("roxy")
        )
        purchase.refresh_from_db()
        # Already cancelled -- order_cancel bails before touching anything.
        self.assertEqual(purchase.credits_remaining, 2)


class OrderDetailPassRenderingTests(StaffFixtureMixin, PassFixtureMixin, TestCase):
    """Pass purchase / pass redemption orders must render on the existing
    staff order detail page without 500ing (mirrors donations' equivalent
    DonationOrderDashboardRenderingTests)."""

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.client.force_login(self.owner)
        self.event, self.performance, self.tier = self.build_ga_event(
            self.org, self.venue, capacity=10
        )

    def test_pass_purchase_order_renders(self):
        product = self.make_product(self.org, price=Decimal("45.00"), credit_count=3)
        order = self.buy_pass(self.org, product, "buyer@example.com")

        resp = self.client.get(f"/dashboard/orders/{order.token}/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, product.name)
        self.assertContains(resp, "$45.00")

    def test_redemption_order_shows_pass_redemption_summary(self):
        product = self.make_product(self.org, price=Decimal("30.00"), credit_count=3)
        self.buy_pass(self.org, product, "holder@example.com")
        purchase = PassPurchase.objects.get(product=product)
        redemption_order = self.redeem(self.org, self.performance, self.tier, purchase)

        resp = self.client.get(
            f"/dashboard/orders/{redemption_order.token}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pass redemptions")
        self.assertContains(resp, product.name)
        self.assertContains(resp, "1 credit")

    def test_order_list_shows_pass_purchase_row(self):
        product = self.make_product(self.org, price=Decimal("45.00"))
        order = self.buy_pass(self.org, product, "buyer@example.com")
        resp = self.client.get("/dashboard/orders/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, order.token)
