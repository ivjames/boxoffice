"""Storefront + redemption UI for season & flex passes (Phase 3).

Two halves:

- STOREFRONT (pass_list / pass_detail / pass_stub): browse & buy a pass
  outright. Mirrors donations/views.py's /donate/ shape almost exactly --
  charges_enabled routes to a real Stripe Checkout Session
  (payments.services.create_pass_checkout_session); not-yet-connected routes
  to pass_stub, this module's simulated-payment stand-in for Stripe's hosted
  page (same idea as donate_stub / orders.views.checkout_stub).

- REDEMPTION (pass_redeem_start / pass_redeem_exit / pass_redeem): spending
  an ALREADY-OWNED PassPurchase against a cart hold, via
  payments.services.fulfill_hold_with_pass. "Redeem mode" is a session flag
  (see passes.context_processors.REDEEMING_PASS_SESSION_KEY) the guest
  portal turns on for one pass at a time; the buyer then shops normally and
  the cart/checkout templates (via passes/templatetags/pass_tags.py) offer a
  "Redeem with pass" button on any covered hold.

This module is the one place in the passes app allowed to import orders/
payments/guests -- passes.models and passes.services themselves stay money-
path-agnostic (see their docstrings). Mirrors how donations/views.py reaches
into payments + guests without donations/services.py doing so.
"""

import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from guests import services as guest_services
from guests.models import normalize_email
from orders import services as order_services
from orders.models import Hold
from payments import services as payment_services
from tenants.decorators import require_tenant

from . import services as pass_services
from .context_processors import REDEEMING_PASS_SESSION_KEY
from .models import PassPurchase

logger = logging.getLogger(__name__)


def _sign_in_buyer(order, request):
    """Sign the buyer into their guest account on this same request -- the
    passes analogue of donations.views._sign_in_donor / orders.views.
    _sign_in_buyer, so a first-time pass buyer lands on their receipt already
    signed in. No-op if the order has no linked guest."""
    if order.guest_id:
        guest_services.login_guest(request, order.guest)


def _send_receipt_best_effort(order, request):
    """Email the order's receipt, but never let a mail-transport failure 500
    the buyer -- mirrors orders.views._send_tickets_best_effort /
    donations.views._send_receipt_best_effort exactly. Imported locally so
    this module carries no import-time dependency beyond what it needs."""
    from orders.emails import send_order_receipt

    try:
        send_order_receipt(order)
    except Exception:
        logger.exception(
            "Receipt email for order %s could not be sent; the order is still "
            "valid and viewable at its receipt page.",
            order.pk,
        )


def _marketing_opt_in(request):
    """Whether the buyer ticked the marketing-consent checkbox on this POST
    (templates/orders/_marketing_consent.html) -- mirrors orders.views.
    _marketing_opt_in / donations.views._marketing_opt_in exactly."""
    return request.POST.get("marketing_opt_in") in ("on", "1", "true")


def _active_products(organization):
    return pass_services.get_active_products(organization)


# --- storefront: browse & buy ----------------------------------------------


@require_tenant
def pass_list(request):
    """GET-only browsing page: every active pass this org sells. 404s when
    the org has none turned on -- mirrors donations._active_campaign_or_404's
    gate (the nav link is hidden in that case too, via passes_enabled; this
    is the server-side backstop for a stale/guessed link)."""
    products = _active_products(request.organization).order_by("kind", "name")
    if not products.exists():
        raise Http404("No passes are available for this box office.")
    return render(request, "passes/list.html", {"products": products})


@require_tenant
def pass_detail(request, pk):
    """GET shows the pass + a buyer email/name form (mirrors donate's shape).
    POST validates the email and either redirects to a Stripe Checkout
    Session (or its stub-mode stand-in), or -- ENABLE_TEST_CHECKOUT on an org
    that can't charge yet -- direct-fulfills immediately, exactly like
    donate()'s own env-gated shortcut."""
    organization = request.organization
    product = get_object_or_404(
        _active_products(organization), pk=pk
    )

    if request.method == "POST":
        buyer_name = request.POST.get("buyer_name", "").strip()
        buyer_email = normalize_email(request.POST.get("buyer_email", ""))
        if not buyer_email:
            return render(
                request,
                "passes/detail.html",
                {
                    "product": product,
                    "error": "Enter an email address for your receipt.",
                    "buyer_name": buyer_name,
                    "buyer_email": buyer_email,
                },
            )

        marketing_opt_in = _marketing_opt_in(request)

        if not organization.stripe_charges_enabled and settings.ENABLE_TEST_CHECKOUT:
            order = payment_services.fulfill_pass_purchase(
                organization,
                product=product,
                buyer_email=buyer_email,
                buyer_name=buyer_name,
                provider="test",
                payment_ref=f"test-{uuid.uuid4()}",
                marketing_opt_in=marketing_opt_in,
            )
            _sign_in_buyer(order, request)
            _send_receipt_best_effort(order, request)
            return redirect("ticket_detail", token=order.token)

        url = payment_services.create_pass_checkout_session(
            organization,
            product=product,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            request=request,
            marketing_opt_in=marketing_opt_in,
        )
        return redirect(url)

    guest = guest_services.get_current_guest(request)
    prefill_email = guest.email if guest is not None else ""
    return render(
        request, "passes/detail.html", {"product": product, "buyer_email": prefill_email}
    )


@require_tenant
def pass_stub(request):
    """SIMULATED pass-payment page -- the /passes/ analogue of
    orders.views.checkout_stub / donations.views.donate_stub, for a tenant
    that can't take real payments yet. create_pass_checkout_session redirects
    here instead of to Stripe's hosted page. No Stripe call of any kind is
    made and no card is charged.

    GATE mirrors checkout_stub/donate_stub: once a tenant finishes Connect
    onboarding, this 404s so a buyer can't mint a free "paid" pass on a live
    tenant."""
    organization = request.organization
    if organization.stripe_charges_enabled:
        raise Http404("Stub pass checkout is unavailable once this box office can take real payments.")

    product_id = (
        request.POST.get("product_id") if request.method == "POST" else request.GET.get("product_id")
    )
    product = get_object_or_404(_active_products(organization), pk=product_id)

    if request.method == "POST":
        buyer_name = request.POST.get("buyer_name", "").strip()
        buyer_email = normalize_email(request.POST.get("buyer_email", ""))
        if not buyer_email:
            messages.error(request, "Enter an email address for your receipt.")
            return redirect(f"{reverse('pass_stub')}?product_id={product.pk}")

        order = payment_services.fulfill_pass_purchase(
            organization,
            product=product,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            provider="stub",
            payment_ref=f"stub-{uuid.uuid4()}",
            marketing_opt_in=_marketing_opt_in(request),
        )
        _sign_in_buyer(order, request)
        _send_receipt_best_effort(order, request)
        return redirect("ticket_detail", token=order.token)

    return render(request, "passes/stub.html", {"product": product})


# --- redemption: spend an owned pass ----------------------------------------


@require_tenant
@require_POST
def pass_redeem_start(request):
    """Turn on redeem mode for one of the signed-in guest's own passes:
    stash its pk in the session (read by every page render via
    passes.context_processors.pass_nav) and bounce to the storefront to shop.
    Requires a signed-in guest (a pass belongs to a guest account, not an
    anonymous session) and re-checks redeemable_now before committing --
    an exhausted/expired/refunded pass never enters redeem mode."""
    guest = guest_services.get_current_guest(request)
    if guest is None:
        messages.error(request, "Sign in to your account to redeem a pass.")
        return redirect("guest_portal")

    pass_purchase = get_object_or_404(
        PassPurchase.objects.filter(organization=request.organization, guest=guest)
        .select_related("product"),
        pk=request.POST.get("pass_id"),
    )
    if not pass_services.redeemable_now(pass_purchase):
        messages.error(request, "This pass can't be redeemed right now.")
        return redirect("guest_portal")

    request.session[REDEEMING_PASS_SESSION_KEY] = pass_purchase.pk
    product_name = pass_purchase.product.name if pass_purchase.product_id else "your pass"
    messages.success(request, f"Redeeming with {product_name} — pick a show to use it on.")
    return redirect("home")


@require_tenant
@require_POST
def pass_redeem_exit(request):
    """Turn redeem mode off. A plain session pop -- nothing to undo on the
    money side (nothing was spent while just browsing in redeem mode)."""
    request.session.pop(REDEEMING_PASS_SESSION_KEY, None)
    messages.info(request, "Exited pass redemption mode.")
    return redirect("cart")


@require_tenant
@require_POST
def pass_redeem(request):
    """Spend the session's redeeming pass on `hold_id`'s seats. Mirrors
    orders.views.checkout_test's hold lookup (org + this session's own hold,
    not-yet-expired) so a redeem POST can no more reach another session's or
    tenant's hold than a real checkout can. Every entitlement fact is
    re-checked authoritatively inside payments.services.fulfill_hold_with_pass
    under a row lock -- this view's own guest/pass lookups are just the
    "who is this pass for" resolution, not a second source of truth."""
    guest = guest_services.get_current_guest(request)
    if guest is None:
        messages.error(request, "Sign in to your account to redeem a pass.")
        return redirect("guest_portal")

    session_key = order_services.get_session_key(request)
    hold = get_object_or_404(
        Hold,
        pk=request.POST.get("hold_id"),
        organization=request.organization,
        session_key=session_key,
        expires_at__gt=timezone.now(),
    )

    pass_id = request.session.get(REDEEMING_PASS_SESSION_KEY)
    pass_purchase = None
    if pass_id:
        pass_purchase = PassPurchase.objects.filter(
            organization=request.organization, pk=pass_id, guest=guest
        ).first()
    if pass_purchase is None:
        request.session.pop(REDEEMING_PASS_SESSION_KEY, None)
        messages.error(request, "Your pass redemption session expired. Please start again.")
        return redirect("cart")

    try:
        order = payment_services.fulfill_hold_with_pass(
            hold, pass_purchase, buyer_email=guest.email, buyer_name=guest.name
        )
    except (payment_services.PassRedemptionError, payment_services.HoldGoneError) as exc:
        messages.error(request, str(exc))
        return redirect("cart")

    request.session.pop(REDEEMING_PASS_SESSION_KEY, None)
    _send_receipt_best_effort(order, request)
    messages.success(request, "Redeemed! Here are your tickets.")
    return redirect("ticket_detail", token=order.token)
