"""HTTP-layer tests for the single platform Connect webhook endpoint:
signature verification against the ONE platform secret, resolving the
connected account (event.account) back to its Organization, idempotency, hold
re-validation, and account.updated status sync.

stripe.Webhook.construct_event is monkeypatched to a small HMAC-free stand-in
so no real Stripe signing machinery or network call is needed. Our fake
"signature" is just `sig-for-<the secret it was signed with>` -- enough to
exercise "was this signed with the platform webhook secret" without needing
genuine Stripe signing, which is what real verification also boils down to.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

import stripe

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from orders import services as order_services
from orders.models import Hold, Order
from orders.tests import OrdersFixtureMixin
from venues.tests import make_org

# The one platform webhook secret every Connect delivery is verified against,
# and the connected-account id wired onto the test org so event.account
# resolves back to it.
PLATFORM_WEBHOOK_SECRET = "whsec_platform"
ACCT_A = "acct_org_a"
GOOD_SIG = f"sig-for-{PLATFORM_WEBHOOK_SECRET}"


def fake_construct_event(payload, sig_header, secret, **kwargs):
    if sig_header != f"sig-for-{secret}":
        raise stripe.error.SignatureVerificationError("bad signature", sig_header)
    return json.loads(payload)


@override_settings(STRIPE_WEBHOOK_SECRET=PLATFORM_WEBHOOK_SECRET)
class StripeWebhookViewTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_ga_performance()
        self.org.stripe_account_id = ACCT_A
        self.org.save(update_fields=["stripe_account_id"])
        self.hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=1,
        )

    def _payload(self, hold=None, session_id="cs_test_1", account=ACCT_A):
        hold = hold or self.hold
        return json.dumps(
            {
                "type": "checkout.session.completed",
                "account": account,
                "data": {
                    "object": {
                        "id": session_id,
                        "payment_intent": "pi_1",
                        "metadata": {
                            "hold_id": str(hold.pk),
                            "organization_id": str(hold.organization_id),
                        },
                        "customer_details": {"email": "buyer@example.com", "name": "Buyer"},
                    }
                },
            }
        ).encode()

    def _post(self, data, signature=GOOD_SIG):
        return self.client.post(
            "/webhooks/stripe/",
            data=data,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature,
        )

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_valid_signature_creates_order_and_tickets(self, mock_construct):
        resp = self._post(self._payload())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        self.assertEqual(order.organization_id, self.org.pk)
        self.assertEqual(order.tickets.count(), 1)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 1)
        self.assertFalse(Hold.objects.filter(pk=self.hold.pk).exists())

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_replay_of_same_session_is_idempotent(self, mock_construct):
        payload = self._payload()
        first = self._post(payload)
        second = self._post(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_bad_signature_rejected_and_creates_nothing(self, mock_construct):
        resp = self._post(self._payload(), signature="totally-wrong-signature")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_unknown_connected_account_is_acked_without_fulfillment(self, mock_construct):
        """An event whose `account` maps to no Organization is acknowledged
        (200, so Stripe stops retrying) but fulfills nothing -- e.g. an event
        from an account that was deleted on our side."""
        resp = self._post(self._payload(account="acct_unknown"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_metadata_org_mismatch_is_acked_without_fulfillment(self, mock_construct):
        """Defense-in-depth: if the session's metadata.organization_id doesn't
        match the org resolved from event.account, fulfillment raises
        TenantMismatchError, which the view logs and acks with 200 (retrying
        can't fix it) without creating an order."""
        payload = json.loads(self._payload())
        payload["data"]["object"]["metadata"]["organization_id"] = "999999"
        resp = self._post(json.dumps(payload).encode())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_expired_hold_acknowledged_200_with_no_order_created(self, mock_construct):
        self.hold.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        self.hold.save(update_fields=["expires_at"])

        resp = self._post(self._payload())

        # Acknowledged (200) so Stripe doesn't retry a hold that's gone for
        # good -- but nothing was fulfilled.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_account_updated_syncs_capability_flags(self, mock_construct):
        """account.updated carries the connected account as data.object; the
        view caches its charges_enabled/details_submitted onto the matching
        Organization so the dashboard reflects onboarding progress without a
        manual refresh."""
        self.assertFalse(self.org.stripe_charges_enabled)
        payload = json.dumps(
            {
                "type": "account.updated",
                "account": ACCT_A,
                "data": {
                    "object": {
                        "id": ACCT_A,
                        "charges_enabled": True,
                        "details_submitted": True,
                    }
                },
            }
        ).encode()

        resp = self._post(payload)

        self.assertEqual(resp.status_code, 200)
        self.org.refresh_from_db()
        self.assertTrue(self.org.stripe_charges_enabled)
        self.assertTrue(self.org.stripe_details_submitted)

    def test_get_not_allowed(self):
        resp = self.client.get("/webhooks/stripe/")
        self.assertEqual(resp.status_code, 405)

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_missing_platform_webhook_secret_rejected(self):
        """With no platform webhook secret configured, the endpoint can't
        verify anything and rejects outright rather than trusting a payload."""
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 400)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_no_csrf_token_still_succeeds(self, mock_construct):
        """Stripe's POST carries no CSRF token (it's not a same-site form
        submission) -- the view must be @csrf_exempt or every real webhook
        delivery would 403."""
        csrf_client = self.client_class(enforce_csrf_checks=True)
        resp = csrf_client.post(
            "/webhooks/stripe/",
            data=self._payload(),
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=GOOD_SIG,
        )
        self.assertEqual(resp.status_code, 200)


class ConnectOnboardingViewTests(StaffFixtureMixin, TestCase):
    """The staff-facing Stripe Connect (Express) onboarding endpoints:
    creating/reusing the connected account, redirecting into Stripe's hosted
    onboarding, syncing status on return, and the owner-only (billing) gate.
    Every Stripe SDK call is mocked -- no network.
    """

    def setUp(self):
        self.org = make_org("roxy")
        self.owner, _ = self.make_staff(self.org, Membership.Role.OWNER)

    def _login(self, user):
        self.client.force_login(user)

    @patch("payments.services.stripe.AccountLink.create")
    @patch("payments.services.stripe.Account.create")
    def test_connect_start_creates_account_and_redirects_to_onboarding(self, mock_acct, mock_link):
        mock_acct.return_value = SimpleNamespace(id="acct_new_123")
        mock_link.return_value = SimpleNamespace(url="https://connect.stripe.com/setup/acct_new_123")
        self._login(self.owner)

        resp = self.client.post("/dashboard/payments/connect/", HTTP_HOST=host_for("roxy"))

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://connect.stripe.com/setup/acct_new_123")
        self.org.refresh_from_db()
        self.assertEqual(self.org.stripe_account_id, "acct_new_123")
        # The Account Link was minted for the just-created account.
        self.assertEqual(mock_link.call_args.kwargs["account"], "acct_new_123")

    @patch("payments.services.stripe.AccountLink.create")
    @patch("payments.services.stripe.Account.create")
    def test_connect_start_reuses_existing_account(self, mock_acct, mock_link):
        """A theater that abandoned onboarding and comes back resumes its
        existing connected account rather than spawning a second one."""
        self.org.stripe_account_id = "acct_existing"
        self.org.save(update_fields=["stripe_account_id"])
        mock_link.return_value = SimpleNamespace(url="https://connect.stripe.com/setup/acct_existing")
        self._login(self.owner)

        resp = self.client.post("/dashboard/payments/connect/", HTTP_HOST=host_for("roxy"))

        self.assertEqual(resp.status_code, 302)
        mock_acct.assert_not_called()  # no new account created
        self.assertEqual(mock_link.call_args.kwargs["account"], "acct_existing")

    @patch("payments.services.stripe.Account.retrieve")
    def test_connect_return_syncs_status_from_stripe(self, mock_retrieve):
        self.org.stripe_account_id = "acct_return"
        self.org.save(update_fields=["stripe_account_id"])
        mock_retrieve.return_value = SimpleNamespace(
            charges_enabled=True, details_submitted=True
        )
        self._login(self.owner)

        resp = self.client.get(
            "/dashboard/payments/connect/return/", HTTP_HOST=host_for("roxy")
        )

        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertTrue(self.org.stripe_charges_enabled)
        self.assertTrue(self.org.stripe_details_submitted)

    def test_connect_start_is_owner_only(self):
        """Connecting payouts is billing_required (owner). A non-owner staffer
        (box office) can't start onboarding."""
        clerk, _ = self.make_staff(
            self.org, Membership.Role.BOX_OFFICE, email="clerk@roxy.test"
        )
        self._login(clerk)

        resp = self.client.post("/dashboard/payments/connect/", HTTP_HOST=host_for("roxy"))

        self.assertEqual(resp.status_code, 403)

    def test_connect_start_requires_login(self):
        resp = self.client.post("/dashboard/payments/connect/", HTTP_HOST=host_for("roxy"))
        # Anonymous -> bounced to login, not executed.
        self.assertIn(resp.status_code, (302, 301))
        self.assertIn("/login", resp["Location"])
