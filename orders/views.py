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
from payments import services as payment_services
from tenants.decorators import require_tenant

from . import services
from .emails import send_ticket_email
from .models import Hold, Order
from .qr import ticket_qr_data_uri


def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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

    context = {"performance": performance, "existing_hold": existing_hold}

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
    items = [{"hold": h, "total": services.hold_total(h)} for h in holds]
    grand_total = sum((item["total"] for item in items), Decimal("0.00"))
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
    items = [{"hold": h, "total": services.hold_total(h)} for h in holds]
    grand_total = sum((item["total"] for item in items), Decimal("0.00"))
    return render(request, "orders/checkout.html", {"items": items, "grand_total": grand_total})


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

    send_ticket_email(order, request)
    return redirect("ticket_detail", token=order.token)


@require_tenant
def checkout_stub(request):
    """SIMULATED hosted-checkout page, used when a tenant has no Stripe keys
    (Organization.stripe_secret_key is blank). create_checkout_session
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
    """
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

        send_ticket_email(order, request)
        return redirect("ticket_detail", token=order.token)

    total = services.hold_total(hold)
    return render(request, "orders/checkout_stub.html", {"hold": hold, "total": total})


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
            .select_related("performance", "performance__event", "performance__venue")
            .filter(stripe_checkout_session_id=session_id)
            .first()
        )
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
    ticket_rows = [{"ticket": ticket, "qr_data_uri": ticket_qr_data_uri(ticket, request)} for ticket in tickets]
    return render(request, "orders/ticket_detail.html", {"order": order, "ticket_rows": ticket_rows})
