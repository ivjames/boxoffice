"""HTTP-layer tests for the Stripe webhook endpoint: signature verification,
idempotency, hold re-validation, and tenant isolation.

stripe.Webhook.construct_event is monkeypatched to a small HMAC-free stand-in
so no real Stripe signing machinery or network call is needed. Our fake
"signature" is just `sig-for-<the secret it was signed with>` -- enough to
exercise "does the org's stored webhook secret match what the payload was
signed with" without needing genuine Stripe signing, which is exactly what
real signature verification also boils down to.
"""

import json
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import stripe

from orders import services as order_services
from orders.models import Hold, Order
from orders.tests import OrdersFixtureMixin
from venues.tests import make_org


def host_for(subdomain):
    return f"{subdomain}.localhost"


def fake_construct_event(payload, sig_header, secret, **kwargs):
    if sig_header != f"sig-for-{secret}":
        raise stripe.error.SignatureVerificationError("bad signature", sig_header)
    return json.loads(payload)


class StripeWebhookViewTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_ga_performance()
        self.org.stripe_webhook_secret = "whsec_org_a"
        self.org.save(update_fields=["stripe_webhook_secret"])
        self.hold = order_services.set_ga_hold(
            organization=self.org,
            performance=self.performance,
            session_key="sess-a",
            user=None,
            price_tier=self.price_tier,
            quantity=1,
        )

    def _payload(self, hold=None, session_id="cs_test_1"):
        hold = hold or self.hold
        return json.dumps(
            {
                "type": "checkout.session.completed",
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

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_valid_signature_creates_order_and_tickets(self, mock_construct):
        resp = self.client.post(
            "/webhooks/stripe/",
            data=self._payload(),
            content_type="application/json",
            HTTP_HOST=host_for(self.org.subdomain),
            HTTP_STRIPE_SIGNATURE="sig-for-whsec_org_a",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        self.assertEqual(order.tickets.count(), 1)
        self.performance.ga_allocation.refresh_from_db()
        self.assertEqual(self.performance.ga_allocation.sold, 1)
        self.assertFalse(Hold.objects.filter(pk=self.hold.pk).exists())

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_replay_of_same_session_is_idempotent(self, mock_construct):
        payload = self._payload()
        kwargs = dict(
            content_type="application/json",
            HTTP_HOST=host_for(self.org.subdomain),
            HTTP_STRIPE_SIGNATURE="sig-for-whsec_org_a",
        )
        first = self.client.post("/webhooks/stripe/", data=payload, **kwargs)
        second = self.client.post("/webhooks/stripe/", data=payload, **kwargs)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_bad_signature_rejected_and_creates_nothing(self, mock_construct):
        resp = self.client.post(
            "/webhooks/stripe/",
            data=self._payload(),
            content_type="application/json",
            HTTP_HOST=host_for(self.org.subdomain),
            HTTP_STRIPE_SIGNATURE="totally-wrong-signature",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_org_bs_secret_cannot_verify_org_as_event(self, mock_construct):
        """A payload signed for org A's webhook secret must NOT verify
        against org B's -- the core of per-tenant webhook isolation."""
        org_b = make_org("org-b")
        org_b.stripe_webhook_secret = "whsec_org_b"
        org_b.save(update_fields=["stripe_webhook_secret"])

        resp = self.client.post(
            "/webhooks/stripe/",
            data=self._payload(),
            content_type="application/json",
            HTTP_HOST=host_for("org-b"),
            HTTP_STRIPE_SIGNATURE="sig-for-whsec_org_a",  # signed for org A's secret, not org B's
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    @patch("payments.views.stripe.Webhook.construct_event", side_effect=fake_construct_event)
    def test_expired_hold_acknowledged_200_with_no_order_created(self, mock_construct):
        self.hold.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        self.hold.save(update_fields=["expires_at"])

        resp = self.client.post(
            "/webhooks/stripe/",
            data=self._payload(),
            content_type="application/json",
            HTTP_HOST=host_for(self.org.subdomain),
            HTTP_STRIPE_SIGNATURE="sig-for-whsec_org_a",
        )

        # Acknowledged (200) so Stripe doesn't retry a hold that's gone for
        # good -- but nothing was fulfilled.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    def test_get_not_allowed(self):
        resp = self.client.get("/webhooks/stripe/", HTTP_HOST=host_for(self.org.subdomain))
        self.assertEqual(resp.status_code, 405)

    def test_org_without_webhook_secret_rejected(self):
        self.org.stripe_webhook_secret = ""
        self.org.save(update_fields=["stripe_webhook_secret"])

        resp = self.client.post(
            "/webhooks/stripe/",
            data=self._payload(),
            content_type="application/json",
            HTTP_HOST=host_for(self.org.subdomain),
            HTTP_STRIPE_SIGNATURE="sig-for-whsec_org_a",
        )
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
            HTTP_HOST=host_for(self.org.subdomain),
            HTTP_STRIPE_SIGNATURE="sig-for-whsec_org_a",
        )
        self.assertEqual(resp.status_code, 200)

    def test_platform_host_rejected(self):
        resp = self.client.post(
            "/webhooks/stripe/",
            data=self._payload(),
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig-for-whsec_org_a",
        )
        self.assertEqual(resp.status_code, 400)
