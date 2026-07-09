import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

import stripe

from orders.emails import send_ticket_email

from . import services

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe posts here on the TENANT subdomain: each Organization's own
    Stripe Dashboard/API webhook endpoint is configured as
    `https://<sub>.<BASE_DOMAIN>/webhooks/stripe/`, so by the time this view
    runs, TenantMiddleware has already resolved `request.organization` from
    the Host header Stripe sent the request to -- that's what tells us which
    org's `stripe_webhook_secret` to verify the signature against. There is
    no platform-wide webhook secret.

    CSRF-exempt: this is an external POST from Stripe, not a same-site form
    submission -- there's no CSRF token to check, and requiring one would
    just make every legitimate delivery fail. The signature verification
    below (not CSRF) is the actual authenticity guarantee.

    Response codes matter here: Stripe treats any non-2xx as "retry later"
    and keeps retrying for up to 3 days. A bad/missing signature (400) is
    something Stripe will never fix by retrying, so that's fine to return
    once. A FulfillmentError (hold gone/expired, availability changed) is
    ALSO something retrying can't fix -- so, per Stripe's own webhook
    guidance, we still acknowledge with 200 after logging it, rather than
    triggering a multi-day retry storm for an event we've already decided we
    can't act on.
    """
    organization = request.organization
    if organization is None or not organization.stripe_webhook_secret:
        return HttpResponse(status=400)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, organization.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        try:
            order, created = services.fulfill_checkout_session(organization, session)
        except services.FulfillmentError:
            logger.exception(
                "checkout.session.completed fulfillment failed for org=%s session=%s",
                organization.pk,
                session.get("id"),
            )
            return HttpResponse(status=200)

        if created:
            send_ticket_email(order, request)

    return HttpResponse(status=200)
