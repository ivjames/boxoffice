"""Tests for the dashboard Promo codes area (dashboard.views.PromoCode*):
manager+ role gating, tenant isolation, and the form-level per-org
uniqueness + percent/fixed value bounds (dashboard.forms.PromoCodeForm).
Setup style mirrors dashboard/test_team.py."""

from decimal import Decimal

from django.test import TestCase

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from dashboard.tests import DashFixtureMixin
from promotions.models import PromoCode


class PromoCodeAccessTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
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
            resp = self.client.get("/dashboard/promos/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_create_get_manager_and_above_only(self):
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/promos/new/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_toggle_manager_and_above_only(self):
        promo = PromoCode.objects.create(
            organization=self.org, code="SAVE10", kind=PromoCode.Kind.PERCENT, value=Decimal("10")
        )
        for user, expected in [
            (self.box_office, 403),
            (self.scanner, 403),
            (self.manager, 302),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.post(
                f"/dashboard/promos/{promo.pk}/toggle/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, expected, user.email)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/dashboard/promos/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])


class PromoCodeCRUDTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def _create(self, **overrides):
        data = {
            "code": "SAVE10",
            "kind": PromoCode.Kind.PERCENT,
            "value": "10.00",
            "currency": "",
            "starts_at": "",
            "ends_at": "",
            "max_redemptions": "",
            "min_order_amount": "",
            "is_active": "on",
        }
        data.update(overrides)
        return self.client.post("/dashboard/promos/new/", data, HTTP_HOST=host_for("roxy"))

    def test_create_normalizes_code_case(self):
        resp = self._create(code="save10")
        promo = PromoCode.objects.get(organization=self.org)
        self.assertEqual(promo.code, "SAVE10")
        self.assertRedirects(resp, "/dashboard/promos/", fetch_redirect_response=False)

    def test_create_is_scoped_to_current_org_even_if_spoofed(self):
        # "organization" isn't a real PromoCodeForm field -- exactly the
        # spoofing guard EventPerformanceCRUDTests exercises for EventForm.
        resp = self._create(organization=self.other_org.pk)
        promo = PromoCode.objects.get(code="SAVE10")
        self.assertEqual(promo.organization_id, self.org.id)
        self.assertRedirects(resp, "/dashboard/promos/", fetch_redirect_response=False)

    def test_list_shows_only_this_orgs_promos(self):
        PromoCode.objects.create(
            organization=self.org, code="MINE", kind=PromoCode.Kind.PERCENT, value=Decimal("5")
        )
        PromoCode.objects.create(
            organization=self.other_org, code="THEIRS", kind=PromoCode.Kind.PERCENT, value=Decimal("5")
        )
        resp = self.client.get("/dashboard/promos/", HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, "MINE")
        self.assertNotContains(resp, "THEIRS")

    def test_update_cross_org_pk_404s(self):
        other_promo = PromoCode.objects.create(
            organization=self.other_org, code="OTHER", kind=PromoCode.Kind.PERCENT, value=Decimal("5")
        )
        resp = self.client.get(f"/dashboard/promos/{other_promo.pk}/edit/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 404)

    def test_toggle_cross_org_pk_404s(self):
        other_promo = PromoCode.objects.create(
            organization=self.other_org, code="OTHER", kind=PromoCode.Kind.PERCENT, value=Decimal("5")
        )
        resp = self.client.post(f"/dashboard/promos/{other_promo.pk}/toggle/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 404)
        other_promo.refresh_from_db()
        self.assertTrue(other_promo.is_active)  # untouched

    def test_edit_updates_fields(self):
        promo = PromoCode.objects.create(
            organization=self.org, code="SAVE10", kind=PromoCode.Kind.PERCENT, value=Decimal("10")
        )
        resp = self.client.post(
            f"/dashboard/promos/{promo.pk}/edit/",
            {
                "code": "SAVE10",
                "kind": PromoCode.Kind.PERCENT,
                "value": "15.00",
                "currency": "",
                "starts_at": "",
                "ends_at": "",
                "max_redemptions": "",
                "min_order_amount": "",
                "is_active": "on",
            },
            HTTP_HOST=host_for("roxy"),
        )
        promo.refresh_from_db()
        self.assertEqual(promo.value, Decimal("15.00"))
        self.assertRedirects(resp, "/dashboard/promos/", fetch_redirect_response=False)

    def test_toggle_deactivates_then_reactivates(self):
        promo = PromoCode.objects.create(
            organization=self.org, code="SAVE10", kind=PromoCode.Kind.PERCENT, value=Decimal("10")
        )
        self.assertTrue(promo.is_active)

        resp = self.client.post(f"/dashboard/promos/{promo.pk}/toggle/", HTTP_HOST=host_for("roxy"))
        self.assertRedirects(resp, "/dashboard/promos/", fetch_redirect_response=False)
        promo.refresh_from_db()
        self.assertFalse(promo.is_active)

        self.client.post(f"/dashboard/promos/{promo.pk}/toggle/", HTTP_HOST=host_for("roxy"))
        promo.refresh_from_db()
        self.assertTrue(promo.is_active)

    def test_duplicate_code_in_same_org_rejected(self):
        PromoCode.objects.create(
            organization=self.org, code="SAVE10", kind=PromoCode.Kind.PERCENT, value=Decimal("10")
        )
        resp = self._create(code="save10")
        self.assertEqual(resp.status_code, 200)  # re-renders form with error
        self.assertContains(resp, "already exists")
        self.assertEqual(PromoCode.objects.filter(organization=self.org).count(), 1)

    def test_same_code_in_other_org_allowed(self):
        PromoCode.objects.create(
            organization=self.other_org, code="SAVE10", kind=PromoCode.Kind.PERCENT, value=Decimal("10")
        )
        resp = self._create(code="SAVE10")
        self.assertRedirects(resp, "/dashboard/promos/", fetch_redirect_response=False)
        self.assertEqual(PromoCode.objects.filter(organization=self.org, code="SAVE10").count(), 1)

    def test_percent_value_must_be_0_to_100(self):
        resp = self._create(kind=PromoCode.Kind.PERCENT, value="150")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "between 0 and 100")
        self.assertFalse(PromoCode.objects.filter(organization=self.org).exists())

    def test_fixed_value_must_be_positive(self):
        resp = self._create(kind=PromoCode.Kind.FIXED, value="0")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "greater than 0")
        self.assertFalse(PromoCode.objects.filter(organization=self.org).exists())
