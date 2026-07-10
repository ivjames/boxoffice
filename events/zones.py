"""Pricing-zone CRUD/mutation service for Phase C of the seating-chart epic
(docs/SEATING.md "C"). Companion to events/pricing.py (read-only price
resolution) -- everything here MUTATES a performance's PricingZone rows.
Every function takes an explicit `organization` and is scoped to it, same
convention as orders/services.py; callers (dashboard views) are responsible
for having already looked up `performance`/`template`/etc. scoped to
`request.organization` via get_object_or_404 before calling in, exactly like
every other manager-gated mutation in this codebase.

"A seat belongs to at most one zone per performance" (PricingZone's
docstring) is enforced here, not at the DB level: `apply_zone` always
removes the requested seats from every OTHER zone on the same performance
before adding them to the target zone, inside one atomic transaction, so the
invariant holds no matter which zone a seat was previously in.
"""

from django.db import transaction

from .models import PricingZone, PricingZoneSeat, ZoneTemplate


class ZoneError(Exception):
    """Base class for zone-mutation failures. Message is safe to show
    directly to staff."""


# docs/EDITOR.md's Round 2 refinement #6 ("seats scale with spacing" bug):
# the seat radius drawn on the map must be a CONSTANT, decoupled from
# seat_pitch/row_pitch -- the pitch sliders should only ever change the GAPS
# between seats, never the seats' own size. This used to be
# `min(section.seat_pitch for ...) * 0.35`, which visibly grew/shrank every
# seat as staff dragged a pitch slider or resize handle. Matches
# static/js/chart_editor.js's SEAT_RADIUS (same value, same units) so the
# live editor and this map's PNG/PDF export (events.zone_export) draw seats
# at the same relative size.
SEAT_RADIUS = 0.35


@transaction.atomic
def apply_zone(*, organization, performance, seat_ids, amount, template):
    """Assign `seat_ids` (Seat pks, already validated as belonging to this
    performance's chart by the caller) to a PricingZone for `performance`,
    creating or reusing the zone instance and snapshotting `template`'s
    current name/color onto it -- see PricingZone's docstring for why this
    is a snapshot, not a live FK read. `template` must already be scoped to
    `organization` (an ad-hoc "new name/color" typed in the editor is turned
    into a real ZoneTemplate first via get_or_create_template below, so
    EVERY zone -- even one defined on the fly -- is reusable on other
    performances afterward, per the epic's locked decision).

    Reuses an existing PricingZone on this performance with the same
    template if one exists (so re-applying to more seats, or changing the
    price, extends/updates that same zone instead of creating a duplicate);
    otherwise creates a new one. Every requested seat is first pulled out of
    any OTHER zone on this same performance, enforcing "at most one zone per
    performance" per seat.

    Returns the PricingZone.
    """
    if not seat_ids:
        raise ZoneError("No seats selected.")

    zone, created = PricingZone.objects.get_or_create(
        organization=organization,
        performance=performance,
        template=template,
        defaults={"name": template.name, "color": template.color, "amount": amount},
    )
    if not created:
        zone.name = template.name
        zone.color = template.color
        zone.amount = amount
        zone.save(update_fields=["name", "color", "amount", "updated_at"])

    # Enforce "at most one zone per performance" for every requested seat --
    # pull it out of any other zone on this performance first.
    PricingZoneSeat.objects.filter(
        zone__performance=performance, seat_id__in=seat_ids
    ).exclude(zone=zone).delete()

    already_in_zone = set(
        PricingZoneSeat.objects.filter(zone=zone, seat_id__in=seat_ids).values_list(
            "seat_id", flat=True
        )
    )
    to_add = [sid for sid in seat_ids if sid not in already_in_zone]
    PricingZoneSeat.objects.bulk_create(
        [
            PricingZoneSeat(organization=organization, zone=zone, seat_id=seat_id)
            for seat_id in to_add
        ]
    )
    return zone


@transaction.atomic
def remove_seats_from_zone(*, organization, zone, seat_ids):
    """Unassign `seat_ids` from `zone` (they become unzoned -- pricing falls
    back to the section PriceTier per events.pricing.resolve_seat_price).
    Does not delete the zone itself even if it ends up with no seats."""
    PricingZoneSeat.objects.filter(
        organization=organization, zone=zone, seat_id__in=seat_ids
    ).delete()


@transaction.atomic
def delete_zone(*, organization, zone):
    """Delete a PricingZone entirely (cascades to its PricingZoneSeat rows).
    Any HoldSeat/OrderItem that already snapshotted this zone's price keeps
    its own `unit_amount` -- see orders/models.py's HoldSeat docstring --
    and the FK to this zone SET_NULLs rather than blocking the delete, since
    the zone itself is disposable once its price has been captured."""
    zone.delete()


@transaction.atomic
def clone_zones_from_performance(*, organization, target_performance, source_performance):
    """Copy every PricingZone (name/color/amount + its seat-set) from
    `source_performance` onto `target_performance` as NEW PricingZone
    instances -- per the epic's locked decision, a performance's zone
    assignment is its own instance; this never mutates `source_performance`
    and future edits to either performance's zones never cross back.

    Only seats that are ALSO part of `target_performance`'s own seating
    chart are copied (a clone across two performances on different charts
    only carries over the seats that exist in both); seats unique to the
    source chart are silently skipped.

    Returns the list of newly created PricingZone instances.
    """
    # Local import: orders.services -> events.pricing is already an
    # existing top-level dependency (orders depends on events); importing
    # orders.services here at module load time would invert that into a
    # cycle, so it's deferred to call time instead, same pattern the rest
    # of this codebase uses for the rare cross-app read that only actually
    # touches app code (not models) that isn't in the load-order path.
    from orders.services import performance_seats

    target_seat_ids = set(performance_seats(target_performance).values_list("pk", flat=True))

    created = []
    source_zones = PricingZone.objects.filter(
        organization=organization, performance=source_performance
    ).prefetch_related("seats")
    for zone in source_zones:
        new_zone = PricingZone.objects.create(
            organization=organization,
            performance=target_performance,
            template=zone.template,
            name=zone.name,
            color=zone.color,
            amount=zone.amount,
        )
        seat_ids = [seat.pk for seat in zone.seats.all() if seat.pk in target_seat_ids]
        PricingZoneSeat.objects.bulk_create(
            [
                PricingZoneSeat(organization=organization, zone=new_zone, seat_id=seat_id)
                for seat_id in seat_ids
            ]
        )
        created.append(new_zone)
    return created


def zone_map_geometry(performance):
    """(sections, seats, seat_radius, view_box) for `performance`'s seating
    chart -- the exact view-box/seat-radius computation
    dashboard.views.performance_pricing_zones turns into the live SVG
    editor's `viewBox`, extracted here so events.zone_export (Phase D's
    static PNG/PDF renderer, docs/SEATING.md "D") computes seat pixel/point
    positions from the SAME numbers and can never visually drift from what
    staff see in the editor. `seats` is every bookable Seat on the chart
    (select_related("section"), ordered); `sections` is the chart's
    Sections in display order; `view_box` is `(min_x, min_y, width,
    height)` in Seat.x/y's own coordinate units (y increases downward, same
    convention the SVG/editor uses), `seat_radius` in those same units.
    Falls back to a fixed 0..10 box when there are no seats yet (a brand
    new/empty chart or a performance with no chart at all), matching the
    editor's own fallback."""
    # Local import: orders.services -> events.pricing is already an
    # existing top-level dependency (orders depends on events); see
    # clone_zones_from_performance above for why this stays call-time
    # instead of a module-level import.
    from orders.services import get_seating_chart, performance_seats

    chart = get_seating_chart(performance)
    sections = list(chart.sections.order_by("ordering", "name")) if chart is not None else []
    seats = list(performance_seats(performance).select_related("section"))

    # A constant, NOT derived from any section's seat_pitch -- see this
    # module's SEAT_RADIUS docstring (docs/EDITOR.md Round 2 refinement #6).
    seat_radius = SEAT_RADIUS
    pad = seat_radius * 4 + 1

    xs = [seat.x for seat in seats]
    ys = [seat.y for seat in seats]
    if xs and ys:
        min_x, max_x = min(xs) - pad, max(xs) + pad
        min_y, max_y = min(ys) - pad, max(ys) + pad
    else:
        min_x = min_y = 0.0
        max_x = max_y = 10.0
    view_box = (min_x, min_y, max_x - min_x, max_y - min_y)
    return sections, seats, seat_radius, view_box


def get_or_create_template(*, organization, name, color):
    """get_or_create a ZoneTemplate by (organization, name) -- used when
    staff type a brand-new name/color on the fly in the zone editor instead
    of picking an existing template (docs/SEATING.md "reusable named/
    colored zone templates -- define once, apply/clone onto any
    performance"): typing a new name defines that template right then, so
    it's immediately available on every other performance too. Reusing an
    existing name updates its color to whatever was just typed."""
    template, created = ZoneTemplate.objects.get_or_create(
        organization=organization, name=name, defaults={"color": color}
    )
    if not created and template.color != color:
        template.color = color
        template.save(update_fields=["color"])
    return template
