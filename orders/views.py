from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from events.models import Performance, PriceTier
from tenants.decorators import require_tenant

from . import services
from .models import Hold, default_hold_expiry


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
        context.update(
            {
                "tiers": list(performance.price_tiers.all()),
                "available": available,
                "existing_quantity": existing_hold.quantity if existing_hold else 0,
            }
        )
    else:
        seats = list(services.performance_seats(performance))
        states = services.reserved_seat_states(performance, session_key=session_key)
        tiers_by_section = services.price_tiers_by_section(performance)

        seats_json = [
            {
                "id": seat.id,
                "row": seat.row_label,
                "number": seat.number,
                "x": seat.x,
                "y": seat.y,
                "section": seat.section.name,
                "state": states.get(seat.id, "unavailable"),
                "price": str(tiers_by_section[seat.section_id].amount)
                if seat.section_id in tiers_by_section
                else None,
                "accessible": seat.is_accessible,
            }
            for seat in seats
        ]
        held_by_you_ids = [s["id"] for s in seats_json if s["state"] == "held_by_you"]
        context.update(
            {
                "seats_json": seats_json,
                "held_by_you_ids": held_by_you_ids,
                "available_count": sum(1 for s in seats_json if s["state"] != "unavailable"),
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
                price_tier = get_object_or_404(
                    PriceTier,
                    pk=request.POST.get("price_tier"),
                    organization=request.organization,
                    performance=performance,
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
        .prefetch_related("hold_seats__seat__section")
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
    """STUB: no Order/Payment/Stripe here (Phase 4). GET renders the current
    hold(s) as an order summary; POST just confirms/refreshes the targeted
    hold's expiry so it survives a bit longer while Phase 4 wires up the
    real "Proceed to payment" action."""
    session_key = services.get_session_key(request)

    if request.method == "POST":
        hold = get_object_or_404(
            Hold,
            pk=request.POST.get("hold_id"),
            organization=request.organization,
            session_key=session_key,
            expires_at__gt=timezone.now(),
        )
        hold.expires_at = default_hold_expiry()
        hold.save(update_fields=["expires_at"])
        messages.info(
            request,
            "Your hold is confirmed and refreshed. Online payment (Stripe) is coming in Phase 4.",
        )
        return redirect("checkout")

    holds = _active_holds(request.organization, session_key)
    items = [{"hold": h, "total": services.hold_total(h)} for h in holds]
    grand_total = sum((item["total"] for item in items), Decimal("0.00"))
    return render(request, "orders/checkout.html", {"items": items, "grand_total": grand_total})
