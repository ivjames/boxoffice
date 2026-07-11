from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from events.models import Event, Performance
from orders import services


def healthz(request):
    return JsonResponse({"status": "ok"})


def _card_pricing_and_availability(performance):
    """Presentation-only helper for the home event cards: the "from $X"
    price and a live availability count for a single (usually the soonest
    upcoming) Performance. Reuses orders.services' existing read-only
    availability/pricing helpers rather than re-deriving the rules here --
    booking logic itself is untouched."""
    if performance.seating_mode == Performance.SeatingMode.GA:
        tiers = list(performance.price_tiers.all())
        available = services.ga_available(performance)
    else:
        tiers = list(services.price_tiers_by_section(performance).values())
        available = services.reserved_available_count(performance)

    min_price = min((t.amount for t in tiers), default=None)
    return min_price, available


def home(request):
    """
    Root URL. Renders the tenant storefront home (published events with at
    least one upcoming, published performance) when request.organization is
    set — i.e. on a real tenant subdomain — otherwise the platform landing
    page (reserved subdomain / bare host) — and does NOT touch tenant data in
    that case, so the platform host never leaks a theater's catalog.
    """
    if request.organization is None:
        return render(request, "tenants/platform_landing.html")

    now = timezone.now()
    events = Event.objects.for_organization(request.organization).filter(
        status=Event.Status.PUBLISHED
    ).prefetch_related("performances")

    events_with_upcoming = []
    for event in events:
        upcoming = sorted(
            (
                p
                for p in event.performances.all()
                if p.status == p.Status.PUBLISHED and p.starts_at >= now
            ),
            key=lambda p: p.starts_at,
        )
        if upcoming:
            min_price, available = _card_pricing_and_availability(upcoming[0])
            events_with_upcoming.append(
                {
                    "event": event,
                    "performances": upcoming,
                    "min_price": min_price,
                    "available": available,
                }
            )

    events_with_upcoming.sort(key=lambda row: row["performances"][0].starts_at)

    return render(
        request,
        "tenants/storefront_home.html",
        {"events_with_upcoming": events_with_upcoming},
    )
