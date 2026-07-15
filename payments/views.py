import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

import stripe

from accounts.permissions import billing_required
from orders.emails import send_order_receipt
from tenants.models import Organization

from . import services

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """The SINGLE platform Connect webhook endpoint. Under Stripe Connect with
    direct charges, events for every theater's connected account are delivered
    to one endpoint on the PLATFORM account and verified against one secret,
    settings.STRIPE_WEBHOOK_SECRET -- not the Host header, and not a per-tenant
    secret (there no longer is one). The connected account each event belongs
    to arrives in the event's top-level `account` field, which we map back to
    an Organization via stripe_account_id.

    CSRF-exempt: this is an external POST from Stripe, not a same-site form
    submission -- there's no CSRF token to check, and requiring one would just
    make every legitimate delivery fail. The signature verification below (not
    CSRF) is the actual authenticity guarantee.

    Response codes matter here: Stripe treats any non-2xx as "retry later" and
    keeps retrying for up to 3 days. A bad/missing signature (400) is something
    Stripe will never fix by retrying, so that's fine to return once. A
    FulfillmentError (hold gone/expired, availability changed) is ALSO
    something retrying can't fix -- so, per Stripe's own webhook guidance, we
    still acknowledge with 200 after logging it, rather than triggering a
    multi-day retry storm for an event we've already decided we can't act on.
    An event for an `account` we don't recognize is likewise acked with 200
    (nothing to do, retrying won't change it).
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponse(status=400)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    event_type = event["type"]

    # account.updated carries the connected account itself as data.object, so
    # resolve the org from that id; every other Connect event we handle is
    # resolved from the event's top-level `account`.
    if event_type == "account.updated":
        account = event["data"]["object"]
        organization = _org_for_account(account.get("id"))
        if organization is not None:
            services.apply_account_status(organization, account)
        return HttpResponse(status=200)

    if event_type == "checkout.session.completed":
        organization = _org_for_account(event.get("account"))
        if organization is None:
            logger.warning(
                "checkout.session.completed for unknown connected account %s; ignoring.",
                event.get("account"),
            )
            return HttpResponse(status=200)

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
            send_order_receipt(order)

    return HttpResponse(status=200)


def _org_for_account(account_id):
    """Map a Stripe connected-account id (acct_…) back to its Organization, or
    None if it's blank/unrecognized. A blank stripe_account_id must never
    match a blank account_id, so guard against the empty case explicitly."""
    if not account_id:
        return None
    return Organization.objects.filter(stripe_account_id=account_id).first()


# --- Connect (Express) onboarding (staff-facing) -------------------------


@billing_required
@require_POST
def connect_start(request):
    """Kick off (or resume) Stripe Connect onboarding for this theater. Creates
    the connected account on first use and redirects the manager to Stripe's
    hosted Express onboarding via a fresh Account Link. Owner-only
    (billing_required / can_manage_billing): connecting the theater's payout
    account decides where its ticket money lands, so it sits behind the same
    gate as the rest of billing."""
    organization = request.organization
    return_url = request.build_absolute_uri(reverse("connect_return"))
    refresh_url = request.build_absolute_uri(reverse("connect_refresh"))
    try:
        url = services.create_onboarding_link(
            organization, return_url=return_url, refresh_url=refresh_url
        )
    except services.ConnectError:
        messages.error(
            request,
            "Couldn't start Stripe onboarding just now. Please try again in a moment.",
        )
        return redirect("dashboard_overview")
    return redirect(url)


@billing_required
def connect_refresh(request):
    """Stripe sends the user here if an Account Link was reused or expired
    before onboarding completed. Mint a fresh link and bounce them straight
    back into onboarding (a GET redirect, no button to click)."""
    organization = request.organization
    return_url = request.build_absolute_uri(reverse("connect_return"))
    refresh_url = request.build_absolute_uri(reverse("connect_refresh"))
    try:
        url = services.create_onboarding_link(
            organization, return_url=return_url, refresh_url=refresh_url
        )
    except services.ConnectError:
        messages.error(request, "Couldn't resume Stripe onboarding. Please try again.")
        return redirect("dashboard_overview")
    return redirect(url)


@billing_required
def connect_return(request):
    """Where Stripe returns the manager after they finish (or step out of)
    Express onboarding. Onboarding completion is asynchronous and ultimately
    confirmed by the account.updated webhook, but we refresh the account's
    status here too so the dashboard reflects it immediately instead of waiting
    on webhook delivery. The Account Link `return_url` carries no proof of
    completion by itself — refresh_account_status re-reads the real state from
    Stripe."""
    organization = request.organization
    enabled = services.refresh_account_status(organization)
    if enabled:
        messages.success(request, "Your Stripe account is connected — you can now sell tickets.")
    else:
        messages.info(
            request,
            "Thanks! Stripe is still reviewing your details. We'll enable ticket "
            "sales automatically once your account is ready.",
        )
    return redirect("dashboard_overview")
