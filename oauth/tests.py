"""Tests for social sign-in (OAuth).

The provider network legs (token exchange + userinfo) are the only parts that
touch the outside world, so every flow test patches `oauth.service.fetch_profile`
to return a chosen OAuthProfile -- nothing here makes a real HTTP call. The
rest is exercised for real: the signed state/nonce, the apex->tenant bounce,
and the account resolution against live Organization/User/Membership/Guest rows.

Multi-host flow in a single-host test client: dev settings have DEBUG=True, so a
tenant is selected with ?_tenant=<sub> and the platform apex is just the bare
host (no _tenant). That's exactly how the real dev flow pivots, so the tests
drive the genuine code path.
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

from django.test import TestCase, override_settings

from accounts.models import Membership, User
from guests.models import GuestAccount
from tenants.models import Organization

from .providers import OAuthProfile, _normalize_facebook, _normalize_google, enabled_providers
from .models import OAuthIdentity

GOOGLE_ON = dict(
    GOOGLE_OAUTH_CLIENT_ID="gid",
    GOOGLE_OAUTH_CLIENT_SECRET="gsecret",
)
# The real dev flow runs on one host (localhost:8000) with the tenant picked by
# ?_tenant=, which the middleware only honors under DEBUG. Django forces
# DEBUG=False in tests, so turn it back on to exercise that genuine dev path
# (single host => the nonce cookie is shared between start and the apex
# callback, exactly as in dev).
FLOW_SETTINGS = dict(DEBUG=True, **GOOGLE_ON)


def profile(**kw):
    base = dict(
        provider="google",
        uid="google-sub-1",
        email="person@example.com",
        email_verified=True,
        name="Pat Person",
        first_name="Pat",
        last_name="Person",
    )
    base.update(kw)
    return OAuthProfile(**base)


class ProviderRegistryTests(TestCase):
    def test_providers_off_by_default(self):
        # No credentials configured -> no buttons, and get_provider is None.
        self.assertEqual(enabled_providers(), [])

    @override_settings(**GOOGLE_ON)
    def test_provider_enabled_with_credentials(self):
        names = [p.name for p in enabled_providers()]
        self.assertEqual(names, ["google"])

    def test_normalize_google(self):
        p = _normalize_google(
            {"sub": "123", "email": "a@b.com", "email_verified": True, "name": "A B",
             "given_name": "A", "family_name": "B"}
        )
        self.assertEqual((p.provider, p.uid, p.email, p.email_verified), ("google", "123", "a@b.com", True))

    def test_normalize_facebook_missing_email_is_unverified(self):
        p = _normalize_facebook({"id": "fb1", "name": "No Email"})
        self.assertEqual(p.uid, "fb1")
        self.assertEqual(p.email, "")
        self.assertFalse(p.email_verified)


@override_settings(**FLOW_SETTINGS)
class FlowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Roxy", slug="roxy", subdomain="roxy", contact_email="box@roxy.test"
        )
        self.other = Organization.objects.create(
            name="Strand", slug="strand", subdomain="strand", contact_email="box@strand.test"
        )
        self.staff = User.objects.create_user(email="staff@example.com", password="pw")
        Membership.objects.create(
            user=self.staff, organization=self.org, role=Membership.Role.OWNER
        )

    # --- start ----------------------------------------------------------

    def _start(self, audience="guest", provider="google", tenant="roxy"):
        return self.client.get(
            f"/oauth/{provider}/start/?audience={audience}&_tenant={tenant}"
        )

    def test_start_redirects_to_provider_and_sets_nonce(self):
        resp = self._start(audience="guest")
        self.assertEqual(resp.status_code, 302)
        loc = resp["Location"]
        self.assertTrue(loc.startswith("https://accounts.google.com/o/oauth2/v2/auth"))
        qs = parse_qs(urlparse(loc).query)
        self.assertEqual(qs["client_id"], ["gid"])
        self.assertEqual(
            qs["redirect_uri"], ["http://localhost:8000/oauth/google/callback/"]
        )
        self.assertIn("state", qs)
        self.assertIn("oauth_nonce", resp.cookies)

    def test_start_bad_audience_404(self):
        self.assertEqual(self._start(audience="root").status_code, 404)

    def test_start_unconfigured_provider_404(self):
        self.assertEqual(self._start(provider="facebook").status_code, 404)

    def test_start_requires_tenant(self):
        # No _tenant on the bare host -> platform host -> require_tenant 404s.
        self.assertEqual(
            self.client.get("/oauth/google/start/?audience=guest").status_code, 404
        )

    # --- callback + complete (helpers) ---------------------------------

    def _state_from_start(self, audience, tenant="roxy"):
        resp = self._start(audience=audience, tenant=tenant)
        return parse_qs(urlparse(resp["Location"]).query)["state"][0]

    def _callback(self, state, code="abc"):
        # Runs on the apex (no _tenant); nonce cookie rides along from start.
        return self.client.get(
            "/oauth/google/callback/?" + urlencode({"code": code, "state": state})
        )

    # --- guest flow -----------------------------------------------------

    def test_guest_signup_creates_and_signs_in(self):
        state = self._state_from_start("guest")
        with patch("oauth.service.fetch_profile", return_value=profile(email="new@buyer.test")):
            cb = self._callback(state)
        self.assertEqual(cb.status_code, 302)
        self.assertIn("/oauth/complete/", cb["Location"])
        self.assertIn("_tenant=roxy", cb["Location"])

        done = self.client.get(cb["Location"])  # redeem on tenant host
        self.assertEqual(done.status_code, 302)
        # A GuestAccount was created for this org, and the session carries it.
        guest = GuestAccount.objects.get(organization=self.org, email="new@buyer.test")
        self.assertEqual(self.client.session["guest_account_id"], guest.pk)

    def test_guest_unverified_email_rejected(self):
        state = self._state_from_start("guest")
        with patch("oauth.service.fetch_profile", return_value=profile(email_verified=False)):
            cb = self._callback(state)
        self.assertIn("oauth_error=no_email", cb["Location"])
        self.assertFalse(GuestAccount.objects.filter(organization=self.org).exists())

    # --- staff flow -----------------------------------------------------

    def test_staff_with_membership_signs_in_and_links_identity(self):
        state = self._state_from_start("staff")
        with patch("oauth.service.fetch_profile", return_value=profile(email="staff@example.com")):
            cb = self._callback(state)
        self.assertIn("/oauth/complete/", cb["Location"])
        done = self.client.get(cb["Location"])
        self.assertEqual(done.status_code, 302)
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(self.staff.pk))
        # The external identity is now linked for next time.
        self.assertTrue(
            OAuthIdentity.objects.filter(provider="google", uid="google-sub-1", user=self.staff).exists()
        )

    def test_staff_without_account_rejected(self):
        state = self._state_from_start("staff")
        with patch("oauth.service.fetch_profile", return_value=profile(email="stranger@example.com")):
            cb = self._callback(state)
        self.assertIn("oauth_error=no_account", cb["Location"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_staff_wrong_tenant_rejected(self):
        # Staff has a Membership at roxy, not strand.
        state = self._state_from_start("staff", tenant="strand")
        with patch("oauth.service.fetch_profile", return_value=profile(email="staff@example.com")):
            cb = self._callback(state)
        self.assertIn("oauth_error=no_access", cb["Location"])

    def test_identity_match_by_uid_survives_email_change(self):
        OAuthIdentity.objects.create(provider="google", uid="google-sub-1", user=self.staff, email="old@x")
        state = self._state_from_start("staff")
        # Provider now reports a different email, but the same uid -> same user.
        with patch("oauth.service.fetch_profile", return_value=profile(email="brand-new@example.com")):
            cb = self._callback(state)
        done = self.client.get(cb["Location"])
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(self.staff.pk))

    # --- security -------------------------------------------------------

    def test_nonce_mismatch_rejected(self):
        state = self._state_from_start("guest")
        self.client.cookies.pop("oauth_nonce", None)  # drop the browser binding
        with patch("oauth.service.fetch_profile", return_value=profile()) as m:
            cb = self._callback(state)
        self.assertIn("oauth_error=state", cb["Location"])
        m.assert_not_called()  # never even talked to the provider

    def test_tampered_state_dead_ends_on_apex(self):
        self._state_from_start("guest")  # seed the nonce cookie
        cb = self._callback("not-a-real-signed-state")
        self.assertEqual(cb.status_code, 400)

    def test_provider_denied_bounces_error(self):
        state = self._state_from_start("guest")
        resp = self.client.get(
            "/oauth/google/callback/?"
            + urlencode({"error": "access_denied", "state": state})
        )
        self.assertIn("oauth_error=denied", resp["Location"])

    def test_completion_token_org_mismatch_rejected(self):
        # A completion token minted for `other` must not sign in on `roxy`.
        from . import state as state_mod

        token = state_mod.make_completion(
            audience="guest", org_id=self.other.pk, account_id=1, next_url=""
        )
        resp = self.client.get(
            "/oauth/complete/?" + urlencode({"token": token, "_tenant": "roxy"})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("guest_account_id", self.client.session)


@override_settings(**FLOW_SETTINGS)
class SignInPageTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Roxy", slug="roxy", subdomain="roxy", contact_email="box@roxy.test"
        )

    def test_guest_portal_shows_provider_button(self):
        resp = self.client.get("/account/?_tenant=roxy")
        self.assertContains(resp, "Continue with Google")

    def test_staff_login_shows_provider_button_and_error(self):
        resp = self.client.get("/login/?_tenant=roxy&oauth_error=no_access")
        self.assertContains(resp, "Continue with Google")
        self.assertContains(resp, "doesn&#x27;t have access")
