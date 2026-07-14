"""Standalone /donate/ page: a gift with no hold, no cart, no ticket --
reachable any time donations are switched on for this tenant. The cart
add-on on templates/orders/cart.html (orders/views.py's donation_add /
donation_remove) is the OTHER way a buyer can give, layered onto a ticket
purchase; this is the direct route for a donor who never buys a ticket at
all. Both give to the same org-wide general-fund campaign
(donations.services.get_or_create_general_fund).

GATE: every view here 404s unless the org's general-fund campaign is
currently ACTIVE (is_active doubles as the donations enable flag -- see
DonationCampaign's docstring), mirroring the cart add-on's own gating (see
cart.html's `donations_enabled` check, sourced from
donations.context_processors.donation_nav) so a tenant that hasn't turned
donations on has no reachable donation page at all, not just a hidden nav
link.

Fulfillment mirrors orders/views.py's checkout_stub/checkout_test shape
exactly, just against payments.services.fulfill_donation instead of
fulfill_hold (there's no Hold to re-validate here): stripe_charges_enabled
-> a real Checkout Session (payments.services.create_donation_checkout_session
handles the branching, including its own STUB MODE redirect when charges
aren't enabled yet -- see that function's docstring); the direct-fulfill
ENABLE_TEST_CHECKOUT path below only fires when the org additionally can't
charge, exactly like orders.views.checkout_test's env gate.
"""

import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from guests import services as guest_services
from guests.models import normalize_email
from payments import services as payment_services
from tenants.decorators import require_tenant

from .models import DonationCampaign

logger = logging.getLogger(__name__)

# Sanity ceiling on a standalone gift -- mirrors orders.services.set_hold_donation's
# MAX_HOLD_DONATION (the cart add-on's own cap) so both donation entry points
# agree on the same fat-finger/abuse guard. Kept as an independent constant
# (not imported) since donations must not import orders -- see donations/
# services.py's module docstring on dependency direction.
MAX_DONATION = Decimal("10000")


def _parse_amount(raw):
    """(amount, error) for a submitted donation amount: a positive, 2dp
    Decimal no greater than MAX_DONATION, or a buyer-safe error message.
    Mirrors orders.services.set_hold_donation's own validation exactly, so a
    gift entered here and one added at the cart are held to the same rule."""
    raw = (raw or "").strip()
    try:
        amount = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return None, "Please enter a valid donation amount."
    amount = amount.quantize(Decimal("0.01"))
    if amount <= Decimal("0.00"):
        return None, "Please enter a donation amount greater than zero."
    if amount > MAX_DONATION:
        return (
            None,
            f"Donations here are capped at {MAX_DONATION:.0f}; please contact "
            "the box office for a larger gift.",
        )
    return amount, None


def _marketing_opt_in(request):
    """Whether the donor ticked the marketing-consent checkbox on this POST
    (templates/orders/_marketing_consent.html) -- mirrors orders.views.
    _marketing_opt_in / passes.views._marketing_opt_in exactly."""
    return request.POST.get("marketing_opt_in") in ("on", "1", "true")


def _active_campaign_or_404(organization):
    """The org's active general-fund campaign, or Http404 -- the one gate
    every view in this module opens with. Filters rather than calling
    get_or_create_general_fund (donations.services) so a tenant that has
    never turned donations on is never silently switched on by a stray GET
    -- same read-only stance as donation_nav (donations/context_processors.py)."""
    campaign = (
        DonationCampaign.objects.filter(organization=organization, is_active=True)
        .order_by("created_at", "pk")
        .first()
    )
    if campaign is None:
        raise Http404("Donations aren't turned on for this box office.")
    return campaign


def _sign_in_donor(order, request):
    """Sign the donor into their guest account on this same request -- the
    donation analogue of orders.views._sign_in_buyer, so a first-time donor
    lands on their receipt already able to see it (and any other orders under
    this email) in the guest portal without a magic-link round-trip."""
    if order.guest_id:
        guest_services.login_guest(request, order.guest)


def _send_receipt_best_effort(order, request):
    """Email the donation receipt, but never let a mail-transport failure
    500 the donor -- mirrors orders.views._send_tickets_best_effort exactly.
    Imported locally to keep this module importable even if orders.emails
    changes shape; donations itself carries no hard import-time dependency
    on orders (only this view layer reaches into it, not donations/services.py
    -- see that module's dependency-direction docstring)."""
    from orders.emails import send_donation_receipt_email

    try:
        send_donation_receipt_email(order, request)
    except Exception:
        logger.exception(
            "Donation receipt email for order %s could not be sent; the "
            "donation is still valid and viewable at its receipt page.",
            order.pk,
        )


@require_tenant
def donate(request):
    """GET renders the give form (presets + custom amount + name/email).
    POST validates and either redirects to a Stripe Checkout Session (or its
    stub-mode stand-in -- see create_donation_checkout_session), or, on an
    org that can't charge yet with ENABLE_TEST_CHECKOUT on, direct-fulfills
    immediately with a synthetic payment the same way orders.views.checkout_test
    does for tickets."""
    organization = request.organization
    campaign = _active_campaign_or_404(organization)

    if request.method == "POST":
        amount, error = _parse_amount(request.POST.get("amount"))
        buyer_name = request.POST.get("buyer_name", "").strip()
        buyer_email = normalize_email(request.POST.get("buyer_email", ""))
        if error is None and not buyer_email:
            error = "Enter an email address for your receipt."
        if error:
            return render(
                request,
                "donations/donate.html",
                {
                    "campaign": campaign,
                    "error": error,
                    "amount": request.POST.get("amount", ""),
                    "buyer_name": buyer_name,
                    "buyer_email": buyer_email,
                },
            )

        marketing_opt_in = _marketing_opt_in(request)

        if not organization.stripe_charges_enabled and settings.ENABLE_TEST_CHECKOUT:
            # Same env-gated, org-can't-charge-yet shortcut as checkout_test:
            # skip even the simulated stub and fulfill immediately.
            order = payment_services.fulfill_donation(
                organization,
                amount=amount,
                campaign=campaign,
                buyer_email=buyer_email,
                buyer_name=buyer_name,
                provider="test",
                payment_ref=f"test-{uuid.uuid4()}",
                marketing_opt_in=marketing_opt_in,
            )
            _sign_in_donor(order, request)
            _send_receipt_best_effort(order, request)
            return redirect("ticket_detail", token=order.token)

        url = payment_services.create_donation_checkout_session(
            organization,
            amount=amount,
            campaign=campaign,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            request=request,
            marketing_opt_in=marketing_opt_in,
        )
        return redirect(url)

    return render(request, "donations/donate.html", {"campaign": campaign})


@require_tenant
def donate_stub(request):
    """SIMULATED donation payment page -- the /donate/ analogue of
    orders.views.checkout_stub, for a tenant that can't take real payments
    yet (Organization.stripe_charges_enabled False). create_donation_checkout_session
    redirects here instead of to Stripe's hosted page. No Stripe call of any
    kind is made and no card is charged.

    GET reads `amount` off the query string (as create_donation_checkout_session
    left it) and shows a fake payment form; POST re-validates the amount
    (never trusts the query string as the authoritative charge) and fulfills
    a real, free donation Order with a simulated payment.

    GATE mirrors checkout_stub: once a tenant finishes Connect onboarding,
    this 404s so a buyer can't mint a free "paid" donation order on a live
    tenant."""
    organization = request.organization
    if organization.stripe_charges_enabled:
        raise Http404("Stub donations are unavailable once this box office can take real payments.")
    campaign = _active_campaign_or_404(organization)

    raw_amount = request.POST.get("amount") if request.method == "POST" else request.GET.get("amount")
    amount, error = _parse_amount(raw_amount)

    if request.method == "POST":
        buyer_name = request.POST.get("buyer_name", "").strip()
        buyer_email = normalize_email(request.POST.get("buyer_email", ""))
        if error is None and not buyer_email:
            error = "Enter an email address for your receipt."
        if error:
            return render(
                request,
                "donations/donate_stub.html",
                {"campaign": campaign, "amount": raw_amount, "error": error},
            )

        order = payment_services.fulfill_donation(
            organization,
            amount=amount,
            campaign=campaign,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            provider="stub",
            payment_ref=f"stub-{uuid.uuid4()}",
            marketing_opt_in=_marketing_opt_in(request),
        )
        _sign_in_donor(order, request)
        _send_receipt_best_effort(order, request)
        return redirect("ticket_detail", token=order.token)

    if error:
        messages.error(request, error)
        return redirect(reverse("donate"))

    return render(request, "donations/donate_stub.html", {"campaign": campaign, "amount": amount})
