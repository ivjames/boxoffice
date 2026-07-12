"""Staff auth: login/logout views, and the cross-org isolation guarantee
that's the whole point of Phase 5's auth model -- a user with valid
credentials (and even a live session) for one Organization must not be able
to reach another Organization's staff area. See accounts/permissions.py's
docstring for why that check happens on every request, not just at login.
"""

from django.contrib.auth import get_user_model
from django.core import management
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from tenants.models import Organization
from venues.tests import make_org

from .models import Membership

User = get_user_model()


def host_for(subdomain):
    return f"{subdomain}.localhost"


class StaffFixtureMixin:
    def make_staff(self, organization, role, email=None, password="pw12345!"):
        email = email or f"{role}@{organization.subdomain}.example.com"
        user = User.objects.create_user(email=email, password=password)
        Membership.objects.create(user=user, organization=organization, role=role)
        return user, password


@override_settings(LOGIN_RATELIMIT_MAX_ATTEMPTS=3, LOGIN_RATELIMIT_WINDOW_SECONDS=900)
class LoginThrottleTests(StaffFixtureMixin, TestCase):
    """Cache-backed login throttle (BO-9): repeated failures from one IP get
    locked out; a success clears the counter."""

    def setUp(self):
        cache.clear()  # LocMemCache is shared within the process across tests
        self.org = make_org("roxy")
        self.owner, self.owner_password = self.make_staff(self.org, Membership.Role.OWNER)

    def _bad_login(self):
        return self.client.post(
            "/login/",
            {"email": self.owner.email, "password": "wrong"},
            HTTP_HOST=host_for("roxy"),
        )

    def test_locks_out_after_max_failures(self):
        for _ in range(3):
            resp = self._bad_login()
            self.assertContains(resp, "Incorrect email or password.")
        # 4th attempt is refused before authenticate() even runs.
        resp = self._bad_login()
        self.assertContains(resp, "Too many sign-in attempts")

        # Even correct credentials are refused while locked out.
        resp = self.client.post(
            "/login/",
            {"email": self.owner.email, "password": self.owner_password},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertContains(resp, "Too many sign-in attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_success_clears_the_counter(self):
        self._bad_login()
        self._bad_login()
        # A success resets the count, so the next typo starts fresh (no lockout).
        self.client.post(
            "/login/",
            {"email": self.owner.email, "password": self.owner_password},
            HTTP_HOST=host_for("roxy"),
        )
        self.client.logout()
        resp = self._bad_login()
        self.assertContains(resp, "Incorrect email or password.")
        self.assertNotContains(resp, "Too many sign-in attempts")

    @override_settings(LOGIN_RATELIMIT_MAX_ATTEMPTS=0)
    def test_disabled_when_max_is_zero(self):
        for _ in range(6):
            resp = self._bad_login()
        self.assertContains(resp, "Incorrect email or password.")
        self.assertNotContains(resp, "Too many sign-in attempts")


class LoginViewTests(StaffFixtureMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.org = make_org("roxy")
        self.owner, self.owner_password = self.make_staff(self.org, Membership.Role.OWNER)

    def test_get_renders_login_form(self):
        resp = self.client.get("/login/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Staff sign in")

    def test_login_requires_tenant_host(self):
        resp = self.client.get("/login/")  # no tenant host -> platform host
        self.assertEqual(resp.status_code, 404)

    def test_correct_credentials_with_membership_logs_in(self):
        resp = self.client.post(
            "/login/",
            {"email": self.owner.email, "password": self.owner_password},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(resp, "/dashboard/", fetch_redirect_response=False)
        # Session now authenticates subsequent requests on this org's host.
        dash = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(dash.status_code, 200)

    def test_wrong_password_rejected(self):
        resp = self.client.post(
            "/login/",
            {"email": self.owner.email, "password": "not-the-password"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Incorrect email or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_email_rejected(self):
        resp = self.client.post(
            "/login/",
            {"email": "nobody@example.com", "password": "whatever"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertContains(resp, "Incorrect email or password.")

    def test_correct_credentials_without_any_membership_rejected(self):
        user = User.objects.create_user(email="freelancer@example.com", password="pw12345!")
        resp = self.client.post(
            "/login/",
            {"email": user.email, "password": "pw12345!"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "doesn&#x27;t have access to this theater")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_correct_credentials_for_a_different_orgs_membership_rejected(self):
        """The critical case: a real, valid user -- who works at a DIFFERENT
        theater -- must not be able to log into this one. No session should
        be started at all (as opposed to being started then immediately
        failing a later check)."""
        other_org = make_org("otherorg")
        other_user, other_password = self.make_staff(other_org, Membership.Role.OWNER)

        resp = self.client.post(
            "/login/",
            {"email": other_user.email, "password": other_password},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "doesn&#x27;t have access to this theater")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_redirects_to_next_param_when_safe(self):
        resp = self.client.post(
            "/login/?next=/dashboard/events/",
            {"email": self.owner.email, "password": self.owner_password, "next": "/dashboard/events/"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(resp, "/dashboard/events/", fetch_redirect_response=False)

    def test_login_ignores_unsafe_next_param(self):
        resp = self.client.post(
            "/login/",
            {
                "email": self.owner.email,
                "password": self.owner_password,
                "next": "https://evil.example.com/",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(resp, "/dashboard/", fetch_redirect_response=False)

    def test_scanner_lands_on_scan_not_overview(self):
        """A scanner has no overview -- with no explicit ?next=, login drops
        them straight on the scan screen (mirrors the nav/overview gating)."""
        scanner, password = self.make_staff(self.org, Membership.Role.SCANNER)
        resp = self.client.post(
            "/login/",
            {"email": scanner.email, "password": password},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(resp, reverse("scan_home"), fetch_redirect_response=False)

    def test_box_office_lands_on_overview(self):
        box_office, password = self.make_staff(self.org, Membership.Role.BOX_OFFICE)
        resp = self.client.post(
            "/login/",
            {"email": box_office.email, "password": password},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(resp, "/dashboard/", fetch_redirect_response=False)


class LogoutViewTests(StaffFixtureMixin, TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.owner, self.owner_password = self.make_staff(self.org, Membership.Role.OWNER)
        self.client.post(
            "/login/",
            {"email": self.owner.email, "password": self.owner_password},
            HTTP_HOST=host_for("roxy"),
        )

    def test_logout_clears_session(self):
        resp = self.client.post("/logout/", HTTP_HOST=host_for("roxy"))
        self.assertRedirects(resp, "/login/", fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

        dash = self.client.get("/dashboard/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(dash.status_code, 302)  # bounced to login, not authenticated anymore

    def test_logout_requires_post(self):
        resp = self.client.get("/logout/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 405)


class CrossOrgSessionIsolationTests(StaffFixtureMixin, TestCase):
    """A live, valid session for Org A's staff must not grant access to Org
    B's dashboard, even though Django's test client (like a real browser
    cookie jar, absent SESSION_COOKIE_DOMAIN scoping shenanigans) will
    happily send the same session cookie regardless of which Host header a
    request uses. This is exactly the scenario accounts.permissions guards
    against by re-checking Membership on every request."""

    def setUp(self):
        self.org_a = make_org("org-a")
        self.org_b = make_org("org-b")
        self.user_a, self.password_a = self.make_staff(self.org_a, Membership.Role.OWNER)

    def test_session_from_org_a_cannot_reach_org_bs_dashboard(self):
        self.client.post(
            "/login/",
            {"email": self.user_a.email, "password": self.password_a},
            HTTP_HOST=host_for("org-a"),
        )
        # Same client/session, different tenant host.
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("org-b"))
        self.assertEqual(resp.status_code, 403)

    def test_session_from_org_a_still_works_on_org_a(self):
        self.client.post(
            "/login/",
            {"email": self.user_a.email, "password": self.password_a},
            HTTP_HOST=host_for("org-a"),
        )
        resp = self.client.get("/dashboard/", HTTP_HOST=host_for("org-a"))
        self.assertEqual(resp.status_code, 200)

    def test_user_with_membership_in_both_orgs_can_reach_both(self):
        Membership.objects.create(user=self.user_a, organization=self.org_b, role=Membership.Role.SCANNER)
        self.client.post(
            "/login/",
            {"email": self.user_a.email, "password": self.password_a},
            HTTP_HOST=host_for("org-a"),
        )
        resp_a = self.client.get("/dashboard/", HTTP_HOST=host_for("org-a"))
        resp_b = self.client.get("/dashboard/", HTTP_HOST=host_for("org-b"))
        # Owner in A reaches A's overview; scanner in B is a member too, so
        # they're let into B's staff area -- as a scanner that's the scan
        # screen, not the overview (a non-member would be 403'd, not
        # redirected onward). Both prove the membership grants access.
        self.assertEqual(resp_a.status_code, 200)
        self.assertEqual(resp_b.status_code, 302)
        self.assertEqual(resp_b.headers["Location"], reverse("scan_home"))


class CreateStaffUserCommandTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")

    def test_creates_user_and_membership(self):
        management.call_command(
            "create_staff_user",
            email="new@roxy.example.com",
            password="s3cret-pw",
            org="roxy",
            role="manager",
        )
        user = User.objects.get(email="new@roxy.example.com")
        self.assertTrue(user.check_password("s3cret-pw"))
        membership = Membership.objects.get(user=user, organization=self.org)
        self.assertEqual(membership.role, "manager")

    def test_rerunning_updates_role_without_resetting_password(self):
        management.call_command(
            "create_staff_user", email="new@roxy.example.com", password="first-pw", org="roxy", role="scanner"
        )
        management.call_command(
            "create_staff_user", email="new@roxy.example.com", password="second-pw", org="roxy", role="manager"
        )
        user = User.objects.get(email="new@roxy.example.com")
        membership = Membership.objects.get(user=user, organization=self.org)
        self.assertEqual(membership.role, "manager")
        self.assertTrue(user.check_password("first-pw"))  # not overwritten
        self.assertFalse(user.check_password("second-pw"))

    def test_reset_password_flag_overwrites_password(self):
        management.call_command(
            "create_staff_user", email="new@roxy.example.com", password="first-pw", org="roxy", role="scanner"
        )
        management.call_command(
            "create_staff_user",
            email="new@roxy.example.com",
            password="second-pw",
            org="roxy",
            role="scanner",
            reset_password=True,
        )
        user = User.objects.get(email="new@roxy.example.com")
        self.assertTrue(user.check_password("second-pw"))

    def test_unknown_org_errors(self):
        with self.assertRaises(management.CommandError):
            management.call_command(
                "create_staff_user", email="x@example.com", password="pw", org="nonexistent", role="owner"
            )


class CreateDemoTenantSeedsOwnerTests(TestCase):
    def test_demo_tenant_has_a_logged_in_owner(self):
        management.call_command("create_demo_tenant", subdomain="roxy")
        org = Organization.objects.get(subdomain="roxy")
        membership = Membership.objects.filter(organization=org, role=Membership.Role.OWNER).first()
        self.assertIsNotNone(membership)

        resp = self.client.post(
            "/login/",
            {"email": membership.user.email, "password": "roxy-demo-owner-2026"},
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(resp, "/dashboard/", fetch_redirect_response=False)

    def test_rerunning_does_not_reset_owner_password(self):
        management.call_command("create_demo_tenant", subdomain="roxy")
        org = Organization.objects.get(subdomain="roxy")
        owner_user = Membership.objects.get(organization=org, role=Membership.Role.OWNER).user
        owner_user.set_password("hand-changed-password")
        owner_user.save(update_fields=["password"])

        management.call_command("create_demo_tenant", subdomain="roxy")
        owner_user.refresh_from_db()
        self.assertTrue(owner_user.check_password("hand-changed-password"))
