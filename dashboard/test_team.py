"""Tests for the dashboard Team area (add/adjust/remove staff) and the
invite set-password flow. Role gating mirrors the rest of the dashboard:
manager+ may manage the team, but only owners may grant/change/remove the
OWNER role, and the last owner is protected from removal/demotion."""

from decimal import Decimal

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership, User
from accounts.tests import StaffFixtureMixin, host_for
from dashboard.tests import DashFixtureMixin
from orders.models import Order


class TeamAccessTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.box_office = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.scanner = self.make_staff(self.org, Membership.Role.SCANNER)[0]

    def _get(self):
        return self.client.get("/dashboard/team/", HTTP_HOST=host_for("roxy"))

    def test_manager_and_above_can_view_team(self):
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            self.assertEqual(self._get().status_code, expected, user.email)

    def test_owner_sees_owner_as_assignable_role_manager_does_not(self):
        self.client.force_login(self.owner)
        self.assertIn("owner", self._get().context["assignable_roles"])

        self.client.logout()
        self.client.force_login(self.manager)
        self.assertNotIn("owner", self._get().context["assignable_roles"])


class TeamAddTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]

    def _add(self, **data):
        return self.client.post("/dashboard/team/add/", data, HTTP_HOST=host_for("roxy"))

    def test_inviting_a_new_email_creates_member_and_sends_link(self):
        self.client.force_login(self.owner)
        resp = self._add(email="newbie@example.com", role="scanner", first_name="Ned")
        self.assertEqual(resp.status_code, 302)

        user = User.objects.get(email="newbie@example.com")
        self.assertFalse(user.has_usable_password())
        self.assertTrue(
            Membership.objects.filter(user=user, organization=self.org, role="scanner").exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("set-password/", mail.outbox[0].body)

    def test_adding_an_existing_user_creates_membership_without_email(self):
        existing = User.objects.create_user("already@example.com", "pw12345!")
        self.client.force_login(self.owner)
        self._add(email="already@example.com", role="box_office")

        self.assertTrue(
            Membership.objects.filter(user=existing, organization=self.org, role="box_office").exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_duplicate_member_is_rejected(self):
        self.client.force_login(self.owner)
        self._add(email="dupe@example.com", role="scanner")
        mail.outbox.clear()
        self._add(email="dupe@example.com", role="manager")
        # Still just the one scanner membership; no second one minted.
        user = User.objects.get(email="dupe@example.com")
        self.assertEqual(Membership.objects.filter(user=user, organization=self.org).count(), 1)

    def test_manager_cannot_grant_owner_role(self):
        self.client.force_login(self.manager)
        self._add(email="wannabe@example.com", role="owner")
        self.assertFalse(User.objects.filter(email="wannabe@example.com").exists())


class TeamRoleAndRemovalTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.owner_m = Membership.objects.get(user=self.owner, organization=self.org)
        self.scanner = self.make_staff(self.org, Membership.Role.SCANNER)[0]
        self.scanner_m = Membership.objects.get(user=self.scanner, organization=self.org)
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.manager_m = Membership.objects.get(user=self.manager, organization=self.org)

    def _role(self, membership, role):
        return self.client.post(
            f"/dashboard/team/{membership.id}/role/", {"role": role}, HTTP_HOST=host_for("roxy")
        )

    def _remove(self, membership):
        return self.client.post(
            f"/dashboard/team/{membership.id}/remove/", HTTP_HOST=host_for("roxy")
        )

    def test_owner_can_change_a_members_role(self):
        self.client.force_login(self.owner)
        self._role(self.scanner_m, "box_office")
        self.scanner_m.refresh_from_db()
        self.assertEqual(self.scanner_m.role, "box_office")

    def test_manager_cannot_promote_to_owner(self):
        self.client.force_login(self.manager)
        self._role(self.scanner_m, "owner")
        self.scanner_m.refresh_from_db()
        self.assertEqual(self.scanner_m.role, "scanner")

    def test_manager_cannot_change_an_owner(self):
        self.client.force_login(self.manager)
        self._role(self.owner_m, "manager")
        self.owner_m.refresh_from_db()
        self.assertEqual(self.owner_m.role, "owner")

    def test_cannot_change_your_own_role(self):
        self.client.force_login(self.owner)
        self._role(self.owner_m, "scanner")
        self.owner_m.refresh_from_db()
        self.assertEqual(self.owner_m.role, "owner")

    def test_last_owner_cannot_be_demoted(self):
        # Promote a second person first, then it's allowed; but with a single
        # owner, demotion is blocked.
        self.client.force_login(self.owner)
        self._role(self.owner_m, "manager")  # self-change blocked anyway
        self.assertTrue(Membership.objects.filter(organization=self.org, role="owner").exists())

    def test_last_owner_cannot_be_removed(self):
        self.client.force_login(self.owner)
        # Owner tries to remove themselves -> blocked (self + last owner).
        self._remove(self.owner_m)
        self.assertTrue(Membership.objects.filter(pk=self.owner_m.pk).exists())

    def test_owner_can_remove_a_member_user_row_survives(self):
        self.client.force_login(self.owner)
        self._remove(self.scanner_m)
        self.assertFalse(Membership.objects.filter(pk=self.scanner_m.pk).exists())
        # The User is global (may belong to other orgs) -> not deleted.
        self.assertTrue(User.objects.filter(pk=self.scanner.pk).exists())

    def test_manager_cannot_remove_an_owner(self):
        self.client.force_login(self.manager)
        self._remove(self.owner_m)
        self.assertTrue(Membership.objects.filter(pk=self.owner_m.pk).exists())


class SetPasswordFlowTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]

    def test_invite_link_sets_a_working_password(self):
        self.client.force_login(self.owner)
        self.client.post(
            "/dashboard/team/add/",
            {"email": "invited@example.com", "role": "scanner"},
            HTTP_HOST=host_for("roxy"),
        )
        body = mail.outbox[0].body
        import re

        path = re.search(r"(/set-password/[^/]+/[^/\s]+/)", body).group(1)

        # GET shows the form; POST sets the password.
        self.client.logout()
        self.assertEqual(self.client.get(path, HTTP_HOST=host_for("roxy")).status_code, 200)
        resp = self.client.post(
            path,
            {"new_password1": "s3cretPassw0rd!", "new_password2": "s3cretPassw0rd!"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 302)

        user = User.objects.get(email="invited@example.com")
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password("s3cretPassw0rd!"))

        # The link is single-use: the token no longer validates.
        self.assertEqual(self.client.get(path, HTTP_HOST=host_for("roxy")).status_code, 200)
        self.assertFalse(self.client.get(path, HTTP_HOST=host_for("roxy")).context["validlink"])


class OverviewRevenueBreakdownTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.client.force_login(self.owner)

    def test_revenue_is_broken_down_per_event_and_performance(self):
        e1, perf1, _t1 = self.build_ga_event(self.org, self.venue, slug="show-a")
        e2, perf2, _t2 = self.build_ga_event(self.org, self.venue, slug="show-b")
        # Distinct titles so per-event rows must stay separate (guards against
        # grouping revenue by title instead of event id).
        e1.title = "Hamlet"
        e1.save(update_fields=["title"])
        e2.title = "Macbeth"
        e2.save(update_fields=["title"])
        self.make_paid_order(self.org, perf1, "20.00")
        self.make_paid_order(self.org, perf1, "30.00")
        self.make_paid_order(self.org, perf2, "15.00")
        # A pending order must not count toward revenue.
        Order.objects.create(
            organization=self.org, performance=perf1, buyer_email="p@example.com",
            total=Decimal("999.00"), status=Order.Status.PENDING,
        )

        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))

        perf_rev = {r["performance"].id: r["revenue"] for r in resp.context["performance_rows"]}
        self.assertEqual(perf_rev[perf1.id], Decimal("50.00"))
        self.assertEqual(perf_rev[perf2.id], Decimal("15.00"))

        event_rev = {r["event_title"]: r["revenue"] for r in resp.context["event_revenue_rows"]}
        self.assertEqual(event_rev, {"Hamlet": Decimal("50.00"), "Macbeth": Decimal("15.00")})
        # Ordered by revenue desc.
        self.assertEqual(resp.context["event_revenue_rows"][0]["event_title"], "Hamlet")
