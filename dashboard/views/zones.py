import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from accounts.permissions import manager_required
from events import zones as zone_services
from events.models import Performance, PricingZone, ZoneTemplate
from events.zone_export import ZoneExportError, render_zone_map
from orders.services import get_seating_chart
from venues.models import Seat

from ._common import _section_color


# --- pricing zones (Phase C, docs/SEATING.md) ------------------------------
#
# Per-PERFORMANCE visual pricing-zone editor: marquee (rubber-band) + shift-
# click selection over the same SVG seat elements Phase B's chart editor
# renders (every seat is `<circle class="editor-seat" data-seat-id
# data-section-id>` -- see zone_editor.html/static/js/zone_editor.js), then
# "apply a zone" to the selection (pick an existing ZoneTemplate or define a
# new name+color on the fly -- see events.zones.get_or_create_template),
# remove seats from a zone, delete a zone, or clone another performance's
# zones wholesale. Every endpoint below is manager-gated (ManagerRequired
# via manager_required) and scoped to request.organization + this one
# performance, same tenant-isolation shape as the Phase B save endpoint.


def _zones_payload(performance):
    """[{id, name, color, amount, template_id, seat_ids}] for every
    PricingZone currently on `performance` -- the shape both the initial
    page render (json_script) and every mutation endpoint's JSON response
    share, so the client-side Alpine component can just replace its local
    zones array after any successful mutation instead of re-fetching."""
    zones = (
        PricingZone.objects.filter(organization=performance.organization_id, performance=performance)
        .prefetch_related("seats")
        .order_by("name")
    )
    return [
        {
            "id": zone.pk,
            "name": zone.name,
            "color": zone.color,
            "amount": str(zone.amount),
            "template_id": zone.template_id,
            "seat_ids": [seat.pk for seat in zone.seats.all()],
        }
        for zone in zones
    ]


def _get_org_scoped_performance(request, pk):
    """The Performance for this pk, scoped to request.organization (404
    otherwise -- never leaks a cross-org performance's existence). Callers
    still need to check seating_mode themselves where a redirect (not a 404)
    is the right response for a GA performance (see performance_pricing_zones
    below) vs. a plain 400 for the JSON mutation endpoints."""
    return get_object_or_404(
        Performance.objects.select_related("event", "venue"),
        pk=pk,
        organization=request.organization,
    )


@manager_required
def performance_pricing_zones(request, pk):
    """GET-only: renders the performance's chart as one inline SVG (same
    section-color/seat-radius/view_box computation as chart_editor above,
    reused for continuity between the two editors) plus the zone/template
    data the Alpine component needs -- marquee/shift-click selection state
    and all mutation calls are client-side (static/js/zone_editor.js)."""
    performance = _get_org_scoped_performance(request, pk)
    if performance.seating_mode != Performance.SeatingMode.RESERVED:
        messages.error(request, "Pricing zones are only for reserved-seating performances.")
        return redirect("dashboard_event_detail", pk=performance.event_id)

    # Shared with events.zone_export (Phase D's PNG/PDF export) so the two
    # can never visually drift -- see zone_services.zone_map_geometry.
    sections, seats, seat_radius, (view_min_x, view_min_y, view_w, view_h) = (
        zone_services.zone_map_geometry(performance)
    )

    section_color_by_id = {section.pk: _section_color(i) for i, section in enumerate(sections)}
    for seat in seats:
        seat.editor_color = section_color_by_id.get(seat.section_id, "#6b7280")

    view_box = f"{view_min_x} {view_min_y} {view_w} {view_h}"

    templates = ZoneTemplate.objects.filter(organization=request.organization).order_by("name")
    templates_json = [{"id": t.pk, "name": t.name, "color": t.color} for t in templates]

    other_performances = (
        Performance.objects.filter(
            organization=request.organization, seating_mode=Performance.SeatingMode.RESERVED
        )
        .exclude(pk=performance.pk)
        .select_related("event")
        .order_by("-starts_at")
    )

    return render(
        request,
        "dashboard/zone_editor.html",
        {
            "performance": performance,
            "seats": seats,
            "zones_json": _zones_payload(performance),
            "templates_json": templates_json,
            "other_performances": other_performances,
            "view_box": view_box,
            "seat_radius": seat_radius,
            "apply_url": reverse("dashboard_performance_zone_apply", args=[performance.pk]),
            "remove_url": reverse("dashboard_performance_zone_remove_seats", args=[performance.pk]),
            # zone_editor.js builds each zone's own delete URL from this
            # (reverse() needs a concrete zone pk, but there's one delete URL
            # PER zone, not one per page) by appending `<zone_id>/delete/`.
            "delete_url_prefix": reverse("dashboard_performance_zone_delete", args=[performance.pk, 1])[
                : -len("1/delete/")
            ],
            "clone_url": reverse("dashboard_performance_zone_clone", args=[performance.pk]),
            "export_url": reverse("dashboard_performance_zone_export", args=[performance.pk]),
        },
    )


def _json_body(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _clean_seat_ids(request, performance, raw):
    """Validate `raw` (whatever the client sent as seat_ids) down to the
    subset that are real ints AND actually belong to THIS org's THIS
    performance's chart -- exactly the tenant-isolation shape
    chart_editor_save uses, so a seat id for another org/chart/performance
    is silently dropped, never mutated."""
    if not isinstance(raw, list):
        return []
    try:
        seat_ids = [int(s) for s in raw]
    except (TypeError, ValueError):
        return []
    chart = get_seating_chart(performance)
    if chart is None:
        return []
    return list(
        Seat.objects.filter(
            pk__in=seat_ids, organization=request.organization, section__chart=chart
        ).values_list("pk", flat=True)
    )


@manager_required
@require_POST
def performance_zone_apply(request, pk):
    """Batch-assign the selection to a zone. JSON body:
    `{"seat_ids": [...], "amount": "45.00", "template_id": 5}` to reuse an
    existing ZoneTemplate, OR `{"seat_ids": [...], "amount": "45.00",
    "name": "Premium", "color": "#c1121f"}` to define one on the fly (turned
    into a real, reusable ZoneTemplate via get_or_create_template -- see its
    docstring). Returns the full current zone list so the client can just
    replace its local state."""
    performance = _get_org_scoped_performance(request, pk)
    if performance.seating_mode != Performance.SeatingMode.RESERVED:
        return JsonResponse({"ok": False, "error": "Not a reserved-seating performance."}, status=400)

    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    seat_ids = _clean_seat_ids(request, performance, payload.get("seat_ids"))
    if not seat_ids:
        return JsonResponse({"ok": False, "error": "No valid seats selected."}, status=400)

    try:
        amount = Decimal(str(payload.get("amount", "")).strip())
    except InvalidOperation:
        return JsonResponse({"ok": False, "error": "Enter a valid price."}, status=400)
    if amount < 0:
        return JsonResponse({"ok": False, "error": "Price can't be negative."}, status=400)

    template_id = payload.get("template_id")
    if template_id:
        template = get_object_or_404(ZoneTemplate, pk=template_id, organization=request.organization)
    else:
        name = (payload.get("name") or "").strip()
        color = (payload.get("color") or "").strip()
        if not name or not color:
            return JsonResponse(
                {"ok": False, "error": "Pick an existing zone or give the new one a name and color."},
                status=400,
            )
        template = zone_services.get_or_create_template(
            organization=request.organization, name=name, color=color
        )

    try:
        zone_services.apply_zone(
            organization=request.organization,
            performance=performance,
            seat_ids=seat_ids,
            amount=amount,
            template=template,
        )
    except zone_services.ZoneError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


@manager_required
@require_POST
def performance_zone_remove_seats(request, pk):
    """Unassign seats from a zone. JSON body: `{"zone_id": 3, "seat_ids":
    [...]}`. The zone itself is looked up scoped to org + this performance,
    so a zone id for another performance/org 404s instead of silently doing
    nothing (an explicit staff action deserves a clear error, unlike the
    seat-id list which just gets filtered)."""
    performance = _get_org_scoped_performance(request, pk)
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    zone = get_object_or_404(
        PricingZone, pk=payload.get("zone_id"), organization=request.organization, performance=performance
    )
    seat_ids = _clean_seat_ids(request, performance, payload.get("seat_ids"))
    if not seat_ids:
        return JsonResponse({"ok": False, "error": "No valid seats given."}, status=400)

    zone_services.remove_seats_from_zone(organization=request.organization, zone=zone, seat_ids=seat_ids)
    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


@manager_required
@require_POST
def performance_zone_delete(request, pk, zone_pk):
    """Delete a zone outright (its seats fall back to their section's
    PriceTier -- events.pricing.resolve_seat_price). Any hold/order that
    already snapshotted this zone's price is unaffected -- see HoldSeat's
    docstring."""
    performance = _get_org_scoped_performance(request, pk)
    zone = get_object_or_404(
        PricingZone, pk=zone_pk, organization=request.organization, performance=performance
    )
    zone_services.delete_zone(organization=request.organization, zone=zone)
    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


@manager_required
@require_POST
def performance_zone_clone(request, pk):
    """Clone another performance's zones (incl. seat-sets + prices) onto
    this one as brand-new PricingZone instances -- JSON body:
    `{"source_performance_id": 7}`. The source performance is looked up
    scoped to THIS org only (never another tenant's), but does NOT have to
    share this performance's chart -- events.zones.clone_zones_from_performance
    only copies the seats that are actually part of both."""
    performance = _get_org_scoped_performance(request, pk)
    if performance.seating_mode != Performance.SeatingMode.RESERVED:
        return JsonResponse({"ok": False, "error": "Not a reserved-seating performance."}, status=400)

    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    source_performance = get_object_or_404(
        Performance, pk=payload.get("source_performance_id"), organization=request.organization
    )
    zone_services.clone_zones_from_performance(
        organization=request.organization,
        target_performance=performance,
        source_performance=source_performance,
    )
    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


_EXPORT_CONTENT_TYPES = {"png": "image/png", "pdf": "application/pdf"}


@manager_required
def performance_zone_export(request, pk):
    """GET-only: renders `performance`'s pricing-zone map to PNG or PDF
    (Phase D, docs/SEATING.md "D") and returns it as a download. Org-scoped
    via _get_org_scoped_performance -- a cross-org performance pk 404s
    before any rendering happens, same as every other endpoint on this
    page. Query params (all optional, matching the zone-editor export
    form's field names):

    - `format`: "png" (default) or "pdf".
    - `size`: "letter" (default) or "legal".
    - `labels`: "0" to omit seat row/number labels (default on).
    - `legend`: "0" to omit the zone/price legend (default on).

    Unlike the JSON mutation endpoints above, this isn't restricted to
    RESERVED performances at the HTTP layer -- events.zone_export.render_zone_map
    just renders whatever seats/zones exist (none, for a GA performance)
    and events.zones.zone_map_geometry already falls back to an empty box,
    so a manager who follows a stale link gets a mostly-blank sheet instead
    of a confusing error."""
    performance = _get_org_scoped_performance(request, pk)

    fmt = (request.GET.get("format") or "png").strip().lower()
    size = (request.GET.get("size") or "letter").strip().lower()
    labels = request.GET.get("labels", "1") != "0"
    legend = request.GET.get("legend", "1") != "0"

    try:
        content = render_zone_map(performance, fmt=fmt, size=size, labels=labels, legend=legend)
    except ZoneExportError as exc:
        messages.error(request, str(exc))
        return redirect("dashboard_performance_pricing_zones", pk=performance.pk)

    ext = "pdf" if fmt == "pdf" else "png"
    slug = slugify(f"{performance.event.title}-{performance.starts_at:%Y-%m-%d}") or "performance"
    filename = f"{slug}-pricing-zones.{ext}"

    response = HttpResponse(content, content_type=_EXPORT_CONTENT_TYPES[ext])
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
