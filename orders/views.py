import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from events.models import Performance, PriceTier
from guests import services as guest_services
from guests.models import normalize_email
from payments import services as payment_services
from tenants.decorators import require_tenant

from . import services
from .emails import send_ticket_email
from .models import Hold, Order
from .qr import ticket_qr_data_uri

logger = logging.getLogger(__name__)

# Session key for the buyer's email captured during ticket selection (before
# payment). Pre-fills the checkout forms; the durable account link is made at
# fulfillment off whatever email actually pays.
GUEST_EMAIL_SESSION_KEY = "guest_email"


def _prefill_email(request):
    """Best-known email for pre-filling a checkout form: a signed-in guest's
    own address wins, else whatever was captured during selection, else ''."""
    guest = guest_services.get_current_guest(request)
    if guest is not None:
        return guest.email
    return request.session.get(GUEST_EMAIL_SESSION_KEY, "")


def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sign_in_buyer(order, request):
    """Sign the buyer into their guest account on this same request, so a
    first-time buyer lands on their tickets already able to see (and later
    return to) every order under this email -- no round-trip through the
    magic-link email needed. No-op if the order has no linked guest (e.g. no
    email captured). Only used on the paths where fulfillment happens on the
    buyer's own request (stub/test checkout, and the Stripe success landing);
    the Stripe webhook has no buyer session to sign in."""
    if order.guest_id:
        guest_services.login_guest(request, order.guest)


def _send_tickets_best_effort(order, request):
    """Email the tickets, but NEVER let a mail-transport failure 500 the
    buyer. By the time this runs the Order is already paid and created and
    the tickets are viewable at /tickets/<order.token>/ regardless of email,
    so a broken/unconfigured SMTP host must not turn a successful purchase
    into an error page.

    The stub and test checkout paths call this from the buyer's own "Pay"
    request (unlike the real Stripe path, which emails from the webhook, out
    of band) -- so without this guard an SMTP failure surfaces as a 500 on
    the buyer's click even though nothing about the order failed. We log it
    and move on; the buyer still lands on their tickets."""
    try:
        send_ticket_email(order, request)
    except Exception:
        logger.exception(
            "Ticket email for order %s could not be sent; the order is still "
            "valid and viewable at its tickets page.",
            order.pk,
        )


@require_tenant
def performance_detail(request, pk):
    """GA: quantity selector bounded by live availability. Reserved: an
    interactive seat map. Both show what the CURRENT session already has on
    hold (if anything) so returning to this page doesn't discard a pick."""
    performance = get_object_or_404(
        Performance.objects.for_organization(request.organization).select_related(
            "event", "venue"
        ),
        pk=pk,
        status=Performance.Status.PUBLISHED,
    )
    session_key = services.get_session_key(request)
    existing_hold = services.get_active_hold(request.organization, performance, session_key)

    context = {
        "performance": performance,
        "existing_hold": existing_hold,
        "prefill_email": _prefill_email(request),
    }

    if performance.seating_mode == Performance.SeatingMode.GA:
        available = services.ga_available(performance, exclude_session_key=session_key)
        # GA doesn't assign seats, but the performance still has a house
        # layout (its venue's seating chart). Show it as an INERT map -- a
        # non-interactive picture of the room so the buyer can picture where
        # they'll be -- while the quantity selector below stays the actual
        # purchase control. Seats carry no per-seat state/price here: GA is
        # sold as undifferentiated admission, so every seat renders the same.
        ga_seats_json = [
            {
                "id": seat.id,
                "row": seat.row_label,
                "number": seat.number,
                "x": seat.x,
                "y": seat.y,
                "section": seat.section.name,
            }
            for seat in services.performance_seats(performance)
        ]
        context.update(
            {
                "tiers": list(performance.price_tiers.all()),
                "available": available,
                "existing_quantity": existing_hold.quantity if existing_hold else 0,
                "ga_seats_json": ga_seats_json,
            }
        )
    else:
        seats = list(services.performance_seats(performance))
        states = services.reserved_seat_states(performance, session_key=session_key)
        # Phase C (docs/SEATING.md): a PricingZone wins over the section
        # PriceTier -- resolve_reserved_prices implements that in bulk (see
        # its docstring) so the storefront seat map's price/color always
        # matches what set_reserved_hold will actually charge.
        resolved_prices = services.resolve_reserved_prices(performance)

        seats_json = [
            {
                "id": seat.id,
                "row": seat.row_label,
                "number": seat.number,
                "x": seat.x,
                "y": seat.y,
                "section": seat.section.name,
                "state": states.get(seat.id, "unavailable"),
                "price": str(resolved_prices[seat.id].amount) if seat.id in resolved_prices else None,
                "zone_name": resolved_prices[seat.id].label if seat.id in resolved_prices and resolved_prices[seat.id].is_zone else None,
                "zone_color": resolved_prices[seat.id].color if seat.id in resolved_prices and resolved_prices[seat.id].is_zone else None,
                "accessible": seat.is_accessible,
            }
            for seat in seats
        ]
        held_by_you_ids = [s["id"] for s in seats_json if s["state"] == "held_by_you"]
        context.update(
            {
                "seats_json": seats_json,
                "held_by_you_ids": held_by_you_ids,
                "available_count": sum(
                    1 for s in seats_json if s["state"] not in services.NOT_SELECTABLE_STATES
                ),
            }
        )

    return render(request, "orders/performance_detail.html", context)


@require_tenant
@require_POST
def hold_create(request, pk):
    """Create/replace the session's Hold for this performance. On success,
    redirect to the cart; on a HoldError (sold out / seat just taken / no
    pricing), flash the message and bounce back to the selection page."""
    performance = get_object_or_404(
        Performance.objects.for_organization(request.organization), pk=pk
    )
    session_key = services.get_session_key(request)
    user = request.user if request.user.is_authenticated else None

    try:
        if performance.seating_mode == Performance.SeatingMode.GA:
            quantity = _parse_int(request.POST.get("quantity"), default=0)
            price_tier = None
            if quantity > 0:
                # section__isnull=True keeps this strictly to GA-shaped tiers
                # (performance set, section null) -- a per-performance
                # section override (events/pricing.py) is reserved-seat-only
                # and must never be selectable here even if a client POSTs
                # its pk.
                price_tier = get_object_or_404(
                    PriceTier,
                    pk=request.POST.get("price_tier"),
                    organization=request.organization,
                    performance=performance,
                    section__isnull=True,
                )
            services.set_ga_hold(
                organization=request.organization,
                performance=performance,
                session_key=session_key,
                user=user,
                price_tier=price_tier,
                quantity=quantity,
            )
        else:
            seat_ids = [s for s in request.POST.getlist("seat_id") if s.strip().isdigit()]
            services.set_reserved_hold(
                organization=request.organization,
                performance=performance,
                session_key=session_key,
                user=user,
                seat_ids=seat_ids,
            )
    except services.HoldError as exc:
        messages.error(request, str(exc))
        return redirect("performance_detail", pk=performance.pk)

    # Capture the buyer's email at selection time (optional field on the
    # selection form). Stashed on the session so it pre-fills the checkout
    # forms and is remembered as "who's buying" before payment -- the account
    # itself is created/linked at fulfillment (payments.services.fulfill_hold),
    # keyed off whatever email actually pays.
    captured_email = normalize_email(request.POST.get("buyer_email", ""))
    if captured_email:
        request.session[GUEST_EMAIL_SESSION_KEY] = captured_email

    messages.success(request, "Your selection is on hold for 10 minutes.")
    return redirect("cart")


def _active_holds(organization, session_key):
    return (
        Hold.objects.filter(
            organization=organization,
            session_key=session_key,
            expires_at__gt=timezone.now(),
        )
        .select_related("performance", "performance__event", "performance__venue", "price_tier")
        .prefetch_related("hold_seats__seat__section", "hold_seats__pricing_zone", "hold_seats__price_tier")
        .order_by("expires_at")
    )


@require_tenant
def cart_view(request):
    session_key = services.get_session_key(request)
    holds = _active_holds(request.organization, session_key)
    # `total` stays GROSS (pre-discount) for the per-item line the template
    # shows; `discount` and `net_total` carry the promo math so the UI can show
    # a discount line and the discounted per-hold figure. The page-level
    # grand_total aggregates the NET (what the buyer actually pays). See
    # services.hold_grand_total.
    items = [
        {
            "hold": h,
            "total": services.hold_total(h),
            "discount": services.hold_discount(h),
            "net_total": services.hold_grand_total(h),
        }
        for h in holds
    ]
    grand_total = sum((item["net_total"] for item in items), Decimal("0.00"))
    return render(request, "orders/cart.html", {"items": items, "grand_total": grand_total})


@require_tenant
@require_POST
def cart_release(request):
    session_key = services.get_session_key(request)
    services.release_hold_by_id(
        organization=request.organization,
        session_key=session_key,
        hold_id=request.POST.get("hold_id"),
    )
    messages.info(request, "Hold released.")
    return redirect("cart")


@require_tenant
@require_POST
def promo_apply(request):
    """Apply a promo code to the session's targeted hold. Scoped exactly
    like cart_release above (org + session_key + hold_id from POST) -- a
    request can no more apply a code to another session's or another
    tenant's hold than it can release one. Always redirects to the cart:
    on success the hold's snapshot (Hold.promo_code_text/discount_amount)
    now reflects the applied code, which cart_view already reads to render
    the discount line; on failure services.apply_promo_code raises
    PromoError with a buyer-safe message, flashed instead of a 500/404."""
    session_key = services.get_session_key(request)
    try:
        hold = services.apply_promo_code(
            organization=request.organization,
            session_key=session_key,
            hold_id=request.POST.get("hold_id"),
            code=request.POST.get("code", ""),
        )
    except services.PromoError as exc:
        messages.error(request, str(exc))
        return redirect("cart")
    messages.success(request, f"Code {hold.promo_code_text} applied.")
    return redirect("cart")


@require_tenant
@require_POST
def promo_remove(request):
    """Clear any promo code off the session's targeted hold. Same org/
    session/hold_id scoping as promo_apply/cart_release. Silently no-ops if
    the hold is already gone -- see services.remove_promo_code -- so this
    never surfaces an error for a cart that's already vanished."""
    session_key = services.get_session_key(request)
    services.remove_promo_code(
        organization=request.organization,
        session_key=session_key,
        hold_id=request.POST.get("hold_id"),
    )
    messages.info(request, "Promo code removed.")
    return redirect("cart")


@require_tenant
def checkout_view(request):
    """GET renders the current hold(s) as an order summary. POST creates a
    Stripe Checkout Session for the targeted hold (using THIS org's own
    Stripe secret key -- see payments/services.py) and redirects the
    browser to Stripe's hosted payment page. No Order/Payment/Ticket is
    created here -- that happens once Stripe confirms payment via the
    checkout.session.completed webhook (payments/views.py).
    """
    session_key = services.get_session_key(request)

    if request.method == "POST":
        hold = get_object_or_404(
            Hold,
            pk=request.POST.get("hold_id"),
            organization=request.organization,
            session_key=session_key,
            expires_at__gt=timezone.now(),
        )
        try:
            checkout_url = payment_services.create_checkout_session(hold, request)
        except payment_services.CheckoutError as exc:
            messages.error(request, str(exc))
            return redirect("cart")
        return redirect(checkout_url)

    holds = _active_holds(request.organization, session_key)
    # Same gross/discount/net split as cart_view -- `total` gross for the line,
    # `discount`/`net_total` for the promo display, page grand_total on the net.
    items = [
        {
            "hold": h,
            "total": services.hold_total(h),
            "discount": services.hold_discount(h),
            "net_total": services.hold_grand_total(h),
        }
        for h in holds
    ]
    grand_total = sum((item["net_total"] for item in items), Decimal("0.00"))
    return render(
        request,
        "orders/checkout.html",
        {"items": items, "grand_total": grand_total, "prefill_email": _prefill_email(request)},
    )


@require_tenant
@require_POST
def checkout_test(request):
    """TEST CHECKOUT: env-gated fake-payment path. Reachable ONLY when
    settings.ENABLE_TEST_CHECKOUT is True (checked here, per-request --
    not baked into urls.py at import time -- so it responds correctly to
    the setting being flipped, including in tests via override_settings).
    When the flag is off, this 404s exactly like a URL that doesn't exist,
    and the storefront never shows the button that would POST here (see
    templates/orders/checkout.html + payments/context_processors.py).

    Fulfills the targeted hold IMMEDIATELY, with NO real payment: it calls
    the exact same payments.services.fulfill_hold() core the Stripe webhook
    uses (same re-validate-then-lock, same Order/OrderItem/Ticket creation,
    same GA-sold/seat bookkeeping, same Hold deletion) with provider="test"
    and a synthetic payment_ref -- Order.stripe_checkout_session_id is left
    NULL, so this can never collide with (or be mistaken for) a real Stripe
    order. Overselling/expired-hold rejection is identical to the Stripe
    path because it's the identical code.

    Scoped to THIS org + THIS session's own hold, exactly like every other
    hold-consuming view in this module -- a test-checkout POST can no more
    reach another tenant's or another session's hold than a real checkout
    can.
    """
    if not settings.ENABLE_TEST_CHECKOUT:
        raise Http404("Test checkout is not enabled.")

    session_key = services.get_session_key(request)
    hold = get_object_or_404(
        Hold,
        pk=request.POST.get("hold_id"),
        organization=request.organization,
        session_key=session_key,
        expires_at__gt=timezone.now(),
    )

    buyer_name = request.POST.get("buyer_name", "").strip()
    buyer_email = request.POST.get("buyer_email", "").strip()
    if not buyer_email:
        messages.error(request, "Enter an email address to receive your tickets.")
        return redirect("checkout")

    try:
        order = payment_services.fulfill_hold(
            hold,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            payment_ref=f"test-{uuid.uuid4()}",
            provider="test",
        )
    except payment_services.FulfillmentError as exc:
        messages.error(request, str(exc))
        return redirect("cart")

    _sign_in_buyer(order, request)
    _send_tickets_best_effort(order, request)
    return redirect("ticket_detail", token=order.token)


@require_tenant
def checkout_stub(request):
    """SIMULATED hosted-checkout page, used when a tenant can't take real
    payments yet (Connect onboarding unfinished — Organization.
    stripe_charges_enabled is False). create_checkout_session
    (payments/services.py) redirects the browser here INSTEAD of to Stripe's
    hosted payment page, so "Proceed to payment" works end to end without any
    Stripe account -- this view stands in for Stripe's page. No Stripe call
    of any kind is made and no card is charged.

    GET renders a fake payment form (clearly labelled as simulated). POST
    fulfills the targeted hold with a SIMULATED payment by calling the exact
    same payments.services.fulfill_hold() core the real Stripe webhook uses
    (same re-validate-then-lock, same Order/OrderItem/Ticket creation, same
    GA-sold/seat bookkeeping, same Hold deletion) with provider="stub" and a
    synthetic payment_ref -- Order.stripe_checkout_session_id is left NULL, so
    a stub order can never collide with (or be mistaken for) a real Stripe
    order.

    Scoped to THIS org + THIS session's own hold, exactly like checkout_view
    and checkout_test -- a stub POST can no more reach another tenant's or
    another session's hold than a real checkout can.

    GATE: the stub SIMULATES payment (it hands out real tickets for free), so
    it must only exist for a tenant that genuinely can't charge yet -- exactly
    the condition under which create_checkout_session (payments/services.py)
    routes here. Once a tenant finishes Connect onboarding
    (stripe_charges_enabled True), real checkout goes to Stripe and this
    endpoint 404s, so a buyer can't POST straight to /checkout/stub/ and mint
    free tickets on a live tenant. (checkout_test is the deliberate,
    env-gated way to exercise the free-ticket flow regardless of Connect
    status -- see ENABLE_TEST_CHECKOUT.)
    """
    if request.organization.stripe_charges_enabled:
        raise Http404("Stub checkout is unavailable once the theater can take real payments.")

    session_key = services.get_session_key(request)
    hold = get_object_or_404(
        Hold,
        pk=request.POST.get("hold_id") or request.GET.get("hold_id"),
        organization=request.organization,
        session_key=session_key,
        expires_at__gt=timezone.now(),
    )

    if request.method == "POST":
        buyer_name = request.POST.get("buyer_name", "").strip()
        buyer_email = request.POST.get("buyer_email", "").strip()
        if not buyer_email:
            messages.error(request, "Enter an email address to receive your tickets.")
            return redirect(f"{reverse('checkout_stub')}?hold_id={hold.pk}")

        try:
            order = payment_services.fulfill_hold(
                hold,
                buyer_email=buyer_email,
                buyer_name=buyer_name,
                payment_ref=f"stub-{uuid.uuid4()}",
                provider="stub",
            )
        except payment_services.FulfillmentError as exc:
            messages.error(request, str(exc))
            return redirect("cart")

        _sign_in_buyer(order, request)
        _send_tickets_best_effort(order, request)
        return redirect("ticket_detail", token=order.token)

    # `total` is the GROSS subtotal (unchanged); `discount` and `grand_total`
    # carry the promo math so the simulated payment page shows the same
    # discounted figure a real Stripe page would (grand_total is what the stub
    # "charges" -- it fulfills via hold_grand_total the same way, below).
    total = services.hold_total(hold)
    return render(
        request,
        "orders/checkout_stub.html",
        {
            "hold": hold,
            "total": total,
            "discount": services.hold_discount(hold),
            "grand_total": services.hold_grand_total(hold),
            "prefill_email": _prefill_email(request),
        },
    )


@require_tenant
def checkout_success(request):
    """Stripe redirects here after a successful payment with
    ?session_id={CHECKOUT_SESSION_ID}. The webhook that actually creates the
    Order can lag behind this redirect by a second or two, so: if the Order
    already exists, show it; otherwise show a "we're confirming" state that
    auto-refreshes (see the template) rather than erroring.
    """
    session_id = request.GET.get("session_id")
    order = None
    if session_id:
        order = (
            Order.objects.for_organization(request.organization)
            .select_related("performance", "performance__event", "performance__venue", "guest")
            .filter(stripe_checkout_session_id=session_id)
            .first()
        )
    # The webhook (not this request) created the order + its guest link, so
    # this success landing is where we sign the buyer into their guest
    # account for the Stripe path -- once the order has appeared. If it hasn't
    # yet (webhook lag), the template auto-refreshes and we catch it next load.
    if order is not None:
        _sign_in_buyer(order, request)
    return render(request, "orders/checkout_success.html", {"order": order})


@require_tenant
def checkout_cancel(request):
    """Stripe redirects here if the buyer backs out of the hosted payment
    page. The Hold (if not yet expired) is untouched -- nothing to clean up,
    the buyer can just try checkout again."""
    return render(request, "orders/checkout_cancel.html")


@require_tenant
def ticket_detail(request, token):
    """Public order confirmation + tickets page, scoped to this org and
    reachable only by the unguessable Order.token (no login required) --
    the link sent in the ticket email and shown on /checkout/success/."""
    order = get_object_or_404(
        Order.objects.for_organization(request.organization).select_related(
            "performance", "performance__event", "performance__venue"
        ),
        token=token,
    )
    tickets = list(
        order.tickets.select_related("seat", "seat__section").order_by(
            "seat__section__ordering", "seat__row_label", "seat__number", "id"
        )
    )
    ticket_rows = [{"ticket": ticket, "qr_data_uri": ticket_qr_data_uri(ticket)} for ticket in tickets]
    return render(request, "orders/ticket_detail.html", {"order": order, "ticket_rows": ticket_rows})


@require_tenant
def ticket_pdf(request, token):
    """Downloadable PDF of an order's tickets. Same access model as
    ticket_detail: scoped to this org and reachable only by the unguessable
    Order.token (the token IS the capability -- no login required), so the
    link works straight from the confirmation page, the ticket email, and
    the guest portal alike. See orders.pdf.render_order_pdf."""
    from django.http import HttpResponse

    from .pdf import render_order_pdf

    order = get_object_or_404(
        Order.objects.for_organization(request.organization).select_related(
            "performance", "performance__event", "performance__venue"
        ),
        token=token,
    )
    pdf_bytes = render_order_pdf(order)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="tickets-{order.token}.pdf"'
    return response
