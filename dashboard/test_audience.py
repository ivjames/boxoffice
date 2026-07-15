"""HTTP-level integration tests for the dashboard Audience / CRM area
(dashboard.views.audience_list / audience_detail): manager+ role gating,
tenant isolation, the audience list's order_count/LTV annotations + search/
opt-in/tag filters + CSV export, and the per-guest detail's order history +
editable tags/notes form. Setup style mirrors dashboard/test_passes.py and
dashboard/test_donations.py (build an org, a manager + box_office/scanner
users, log in with the tenant host, assert manager-gating / CSV / isolation).
"""

import csv
import io
from decimal import Decimal

from django.test import TestCase

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from dashboard.tests import DashFixtureMixin
from guests.models import GuestAccount
from orders.models import Order


class AudienceFixtureMixin(DashFixtureMixin):
    def make_guest(self, org, email, *, name="", opt_in=True, tags=""):
        return GuestAccount.objects.create(
            organization=org, email=email, name=name, marketing_opt_in=opt_in, tags=tags
        )

    def paid_order_for(self, org, guest, total, *, status=Order.Status.PAID):
        """A paid order linked to `guest` -- enough to make the audience
        annotations (order_count / ltv, both computed off PAID orders) non-zero.
        No performance/tickets needed: the annotations count/sum Orders, not
        tickets."""
        return Order.objects.create(
            organization=org,
            guest=guest,
            performance=None,
            buyer_email=guest.email,
            buyer_name=guest.name,
            total=Decimal(total),
            status=status,
        )


class AudienceAccessTests(StaffFixtureMixin, AudienceFixtureMixin, TestCase):
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
            resp = self.client.get("/dashboard/audience/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_detail_manager_and_above_only(self):
        guest = self.make_guest(self.org, "g@example.com")
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get(
                f"/dashboard/audience/{guest.pk}/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, expected, user.email)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/dashboard/audience/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])


class AudienceListTests(StaffFixtureMixin, AudienceFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def _rows(self, resp):
        return {g.email: g for g in resp.context["guests"]}

    def test_lists_opted_in_and_opted_out_with_order_count_and_ltv(self):
        spender = self.make_guest(self.org, "spender@example.com", name="Sam Spender", opt_in=True)
        self.paid_order_for(self.org, spender, "35.00")
        self.paid_order_for(self.org, spender, "65.00")
        # A non-PAID order must not inflate the annotations.
        self.paid_order_for(self.org, spender, "999.00", status=Order.Status.PENDING)

        opted_out = self.make_guest(self.org, "out@example.com", opt_in=False)
        self.paid_order_for(self.org, opted_out, "20.00")

        # A guest with no orders at all -- must render with 0/blank, no crash.
        self.make_guest(self.org, "browser@example.com", opt_in=True)

        resp = self.client.get("/dashboard/audience/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        rows = self._rows(resp)
        # Both opted-in AND opted-out guests appear (no opt_in filter by default).
        self.assertIn("spender@example.com", rows)
        self.assertIn("out@example.com", rows)
        self.assertIn("browser@example.com", rows)

        self.assertEqual(rows["spender@example.com"].order_count, 2)
        self.assertEqual(rows["spender@example.com"].ltv, Decimal("100.00"))
        self.assertEqual(rows["out@example.com"].order_count, 1)
        # No-order guest annotates to 0 / NULL rather than exploding.
        self.assertEqual(rows["browser@example.com"].order_count, 0)
        self.assertIsNone(rows["browser@example.com"].ltv)

    def test_search_filters_by_email_or_name(self):
        self.make_guest(self.org, "alice@example.com", name="Alice Adams")
        self.make_guest(self.org, "bob@example.com", name="Bob Brown")

        by_email = self.client.get(
            "/dashboard/audience/?search=alice@", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(list(self._rows(by_email)), ["alice@example.com"])

        by_name = self.client.get(
            "/dashboard/audience/?search=brown", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(list(self._rows(by_name)), ["bob@example.com"])

    def test_opt_in_filter(self):
        self.make_guest(self.org, "in@example.com", opt_in=True)
        self.make_guest(self.org, "out@example.com", opt_in=False)

        opted_in = self.client.get(
            "/dashboard/audience/?opt_in=1", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(set(self._rows(opted_in)), {"in@example.com"})

        opted_out = self.client.get(
            "/dashboard/audience/?opt_in=0", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(set(self._rows(opted_out)), {"out@example.com"})

    def test_tag_filter(self):
        self.make_guest(self.org, "vip@example.com", tags="vip,board")
        self.make_guest(self.org, "plain@example.com")

        resp = self.client.get(
            "/dashboard/audience/?tag=VIP", HTTP_HOST=host_for("roxy")
        )  # case-insensitive substring match
        self.assertEqual(set(self._rows(resp)), {"vip@example.com"})

    def test_tenant_isolation_never_lists_other_orgs_guests(self):
        self.make_guest(self.org, "mine@example.com")
        self.make_guest(self.other_org, "theirs@example.com")

        resp = self.client.get("/dashboard/audience/", HTTP_HOST=host_for("roxy"))
        rows = self._rows(resp)
        self.assertIn("mine@example.com", rows)
        self.assertNotIn("theirs@example.com", rows)
        self.assertNotContains(resp, "theirs@example.com")

    def test_csv_export(self):
        guest = self.make_guest(self.org, "giver@example.com", name="Gina Giver")
        self.paid_order_for(self.org, guest, "42.00")

        resp = self.client.get(
            "/dashboard/audience/?format=csv", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("attachment", resp["Content-Disposition"])

        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(
            rows[0], ["Email", "Name", "Opted in", "Orders", "Lifetime value", "Tags"]
        )
        by_email = {r[0]: r for r in rows[1:]}
        self.assertIn("giver@example.com", by_email)
        data_row = by_email["giver@example.com"]
        self.assertEqual(data_row[1], "Gina Giver")
        self.assertEqual(data_row[2], "yes")
        self.assertEqual(data_row[3], "1")
        self.assertEqual(Decimal(data_row[4]), Decimal("42.00"))

    def test_csv_one_row_per_guest(self):
        self.make_guest(self.org, "a@example.com")
        self.make_guest(self.org, "b@example.com")
        self.make_guest(self.org, "c@example.com")

        resp = self.client.get(
            "/dashboard/audience/?format=csv", HTTP_HOST=host_for("roxy")
        )
        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        # 1 header + 3 guests.
        self.assertEqual(len(rows), 4)


class AudienceDetailTests(StaffFixtureMixin, AudienceFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_get_renders_order_history_and_tags_notes_form(self):
        _event, performance, _tier = self.build_ga_event(self.org, self.venue)
        guest = self.make_guest(self.org, "buyer@example.com", name="Barb Buyer")
        order = Order.objects.create(
            organization=self.org,
            guest=guest,
            performance=performance,
            buyer_email=guest.email,
            total=Decimal("25.00"),
            status=Order.Status.PAID,
        )

        resp = self.client.get(
            f"/dashboard/audience/{guest.pk}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        # The tags/notes form is bound to this guest.
        self.assertEqual(resp.context["form"].instance.pk, guest.pk)
        # The order history includes this guest's paid order.
        order_ids = [row["order"].pk for row in resp.context["order_rows"]]
        self.assertIn(order.pk, order_ids)
        self.assertContains(resp, order.token)

    def test_get_renders_for_guest_with_no_orders_and_blank_name(self):
        guest = self.make_guest(self.org, "empty@example.com", name="")
        resp = self.client.get(
            f"/dashboard/audience/{guest.pk}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["order_rows"], [])

    def test_post_saves_tags_and_notes(self):
        guest = self.make_guest(self.org, "guest@example.com")
        resp = self.client.post(
            f"/dashboard/audience/{guest.pk}/",
            {"tags": "vip, subscriber", "notes": "Comped opening night."},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp, f"/dashboard/audience/{guest.pk}/", fetch_redirect_response=False
        )
        guest.refresh_from_db()
        self.assertEqual(guest.tags, "vip, subscriber")
        self.assertEqual(guest.notes, "Comped opening night.")

    def test_post_never_changes_marketing_opt_in(self):
        # Consent is not editable via this form -- a spoofed marketing_opt_in
        # in the POST body is ignored (not a form field).
        guest = self.make_guest(self.org, "consent@example.com", opt_in=False)
        self.client.post(
            f"/dashboard/audience/{guest.pk}/",
            {"tags": "vip", "notes": "", "marketing_opt_in": "on"},
            HTTP_HOST=host_for("roxy"),
        )
        guest.refresh_from_db()
        self.assertFalse(guest.marketing_opt_in)

    def test_cross_tenant_guest_pk_404s(self):
        other_guest = self.make_guest(self.other_org, "theirs@example.com")
        resp = self.client.get(
            f"/dashboard/audience/{other_guest.pk}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)

    def test_cross_tenant_guest_post_404s_and_saves_nothing(self):
        other_guest = self.make_guest(self.other_org, "theirs@example.com")
        resp = self.client.post(
            f"/dashboard/audience/{other_guest.pk}/",
            {"tags": "hacked", "notes": "hacked"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 404)
        other_guest.refresh_from_db()
        self.assertEqual(other_guest.tags, "")
        self.assertEqual(other_guest.notes, "")
