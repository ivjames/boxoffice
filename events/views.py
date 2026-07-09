from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from orders import services
from tenants.decorators import require_tenant

from .models import Event, Performance


@require_tenant
def event_detail(request, slug):
    """Event page: the show's info plus its upcoming, published
    Performances with a live availability badge on each (GA count via
    services.ga_available, reserved seat count via
    services.reserved_available_count). No session context here — this is a
    shared, cacheable-in-spirit browse page, so GA availability isn't
    narrowed by "exclude my own hold" the way the selection page does.
    """
    event = get_object_or_404(
        Event.objects.for_organization(request.organization),
        slug=slug,
        status=Event.Status.PUBLISHED,
    )

    now = timezone.now()
    performances = (
        Performance.objects.for_organization(request.organization)
        .filter(event=event, status=Performance.Status.PUBLISHED, starts_at__gte=now)
        .select_related("venue")
        .order_by("starts_at")
    )

    performance_rows = []
    for performance in performances:
        if performance.seating_mode == Performance.SeatingMode.GA:
            available = services.ga_available(performance)
        else:
            available = services.reserved_available_count(performance)
        performance_rows.append({"performance": performance, "available": available})

    return render(
        request,
        "events/event_detail.html",
        {"event": event, "performance_rows": performance_rows},
    )
