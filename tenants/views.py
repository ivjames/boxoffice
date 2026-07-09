from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from events.models import Event


def healthz(request):
    return JsonResponse({"status": "ok"})


def home(request):
    """
    Root URL. Renders the tenant storefront home (published events with at
    least one upcoming, published performance) when request.organization is
    set (a tenant subdomain), otherwise the platform landing placeholder
    (reserved subdomain / bare host) — and does NOT touch tenant data in
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
            events_with_upcoming.append((event, upcoming))

    events_with_upcoming.sort(key=lambda pair: pair[1][0].starts_at)

    return render(
        request,
        "tenants/storefront_home.html",
        {"events_with_upcoming": events_with_upcoming},
    )
