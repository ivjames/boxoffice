"""Guest portal marketing-preferences + one-click unsubscribe view coverage
(Phase 4 CRM UI layer).

Owned by the UI-layer agent -- explicitly NOT guests/test_marketing.py (that
file belongs to the concurrent Phase 4 test agent, along with campaigns/
tests.py, dashboard/test_campaigns.py, dashboard/test_audience.py, and the
payments test extensions). This file stays entirely separate from that one.

Reuses orders.test_views' fixtures (TenantClientMixin, StorefrontFixtureMixin)
the same way guests/tests.py and passes/test_views.py do.
"""

from django.test import TestCase

from guests.models import GuestAccount
from guests.tokens import make_unsubscribe_token
from orders.test_views import StorefrontFixtureMixin, TenantClientMixin

from . import services


class GuestPreferencesToggleTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.guest = GuestAccount.objects.create(
            organization=self.org, email="fan@example.com", name="Fan"
        )

    def _sign_in(self):
        session = self.client.session
        session["guest_account_id"] = self.guest.pk
        session["guest_org_id"] = self.org.pk
        session.save()

    def test_requires_sign_in(self):
        resp = self.post_as("org-a", "/account/preferences/", {"marketing_opt_in": "on"})
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.marketing_opt_in)

    def test_signed_in_guest_can_opt_in(self):
        self._sign_in()
        resp = self.post_as("org-a", "/account/preferences/", {"marketing_opt_in": "on"})
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.guest.refresh_from_db()
        self.assertTrue(self.guest.marketing_opt_in)
        self.assertIsNotNone(self.guest.marketing_opt_in_at)

    def test_signed_in_guest_can_opt_out(self):
        services.set_marketing_opt_in(self.guest, True)
        self._sign_in()
        resp = self.post_as("org-a", "/account/preferences/", {})
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.marketing_opt_in)

    def test_portal_shows_current_preference(self):
        services.set_marketing_opt_in(self.guest, True)
        self._sign_in()
        resp = self.get_as("org-a", "/account/")
        self.assertContains(resp, "Subscribed")


class GuestUnsubscribeTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.guest = GuestAccount.objects.create(
            organization=self.org, email="fan@example.com", name="Fan"
        )
        services.set_marketing_opt_in(self.guest, True)

    def test_valid_token_unsubscribes_and_shows_confirm(self):
        token = make_unsubscribe_token(self.guest)
        resp = self.get_as("org-a", f"/account/unsubscribe/?token={token}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "unsubscribed")
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.marketing_opt_in)
        # The opt-in timestamp is retained as an audit trail, not cleared.
        self.assertIsNotNone(self.guest.marketing_opt_in_at)

    def test_invalid_token_shows_friendly_state_without_erroring(self):
        resp = self.get_as("org-a", "/account/unsubscribe/?token=not-a-real-token")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn't valid")
        self.guest.refresh_from_db()
        self.assertTrue(self.guest.marketing_opt_in)  # untouched

    def test_missing_token_shows_friendly_state(self):
        resp = self.get_as("org-a", "/account/unsubscribe/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn't valid")

    def test_token_minted_for_another_org_is_rejected(self):
        other_org, _ = self.build_org("org-b")
        other_guest = GuestAccount.objects.create(organization=other_org, email="fan@example.com")
        token = make_unsubscribe_token(other_guest)
        resp = self.get_as("org-a", f"/account/unsubscribe/?token={token}")
        self.assertContains(resp, "isn't valid")
        self.guest.refresh_from_db()
        self.assertTrue(self.guest.marketing_opt_in)  # org-a's guest untouched

    def test_resubscribe_post_opts_back_in_without_signin(self):
        token = make_unsubscribe_token(self.guest)
        # First, unsubscribe (as a one-click email-link click would).
        self.get_as("org-a", f"/account/unsubscribe/?token={token}")
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.marketing_opt_in)

        # Then resubscribe via the confirm page's POST -- no sign-in, token only.
        resp = self.post_as("org-a", "/account/unsubscribe/", {"token": token})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "subscribed again")
        self.guest.refresh_from_db()
        self.assertTrue(self.guest.marketing_opt_in)

    def test_resubscribe_with_invalid_token_shows_friendly_state(self):
        resp = self.post_as("org-a", "/account/unsubscribe/", {"token": "bogus"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn't valid")
