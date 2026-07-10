"""JSON import/export of a whole SeatingChart (Phase A of the seating-chart
epic, docs/SEATING.md item A). A chart's logical shape (sections -> rows ->
seats, plus each section's layout/authoring params) round-trips losslessly
through `export_chart_data` / `import_chart_data` -- see each function's
docstring for the exact JSON shape. Used by the `export_seating_chart` /
`import_seating_chart` management commands; kept as plain functions here so
they're independently unit-testable without shelling out to `call_command`.

Tenant scoping: both functions are always given an already-resolved,
already-scoped object (`export_chart_data(chart)`, `import_chart_data
(venue, ...)`) -- callers (the management commands) are responsible for
making sure that `chart`/`venue` belongs to the org they mean to touch. No
query in here ever crosses organizations on its own.
"""

from collections import defaultdict

from .models import Seat, SeatingChart, Section


class ChartImportError(Exception):
    """Raised when import_chart_data can't safely import into the target
    venue. Message is safe to show directly (management command / future
    dashboard import UI)."""


def export_chart_data(chart):
    """Serialize `chart` (a SeatingChart) to the plain-dict JSON shape:

        {
          "chart": {"name": "..."},
          "sections": [
            {
              "name": "...", "ordering": 0, "tier": "...",
              "numbering_scheme": "sequential", "row_label_scheme": "skip_io",
              "layout": {
                "origin_x": 0.0, "origin_y": 0.0, "rotation": 0.0,
                "seat_pitch": 1.0, "row_pitch": 1.0, "arc_radius": null,
                "pivot_mode": "center", "pivot_x": 0.0, "pivot_y": 0.0
              },
              "rows": [
                {"row_label": "A", "seats": [
                  {"number": "1", "x": 0.0, "y": 0.0, "accessible": false}, ...
                ]}, ...
              ]
            }, ...
          ]
        }

    Row order within a section is the seat generator's own order (by each
    row's minimum y, tie-broken by label) so a chart built by
    venues.generation.generate_seats round-trips through export/import with
    identical row ordering; a hand-edited chart still gets a deterministic
    order either way. Seats within a row are ordered left-to-right by x.
    """
    sections = []
    for section in chart.sections.order_by("ordering", "name"):
        rows_by_label = defaultdict(list)
        for seat in section.seats.all():
            rows_by_label[seat.row_label].append(seat)

        row_order = sorted(
            rows_by_label.keys(),
            key=lambda label: (min(s.y for s in rows_by_label[label]), label),
        )

        sections.append(
            {
                "name": section.name,
                "ordering": section.ordering,
                "tier": section.tier,
                "numbering_scheme": section.numbering_scheme,
                "row_label_scheme": section.row_label_scheme,
                "layout": {
                    "origin_x": section.origin_x,
                    "origin_y": section.origin_y,
                    "rotation": section.rotation,
                    "seat_pitch": section.seat_pitch,
                    "row_pitch": section.row_pitch,
                    "arc_radius": section.arc_radius,
                    # Round 2's configurable rotation pivot (docs/EDITOR.md
                    # #2) -- round-tripped since round 3 (#12): without
                    # these, an exported/reimported chart with a CUSTOM
                    # pivot would silently reset to the CENTER default and
                    # rotate around the wrong point.
                    "pivot_mode": section.pivot_mode,
                    "pivot_x": section.pivot_x,
                    "pivot_y": section.pivot_y,
                },
                "rows": [
                    {
                        "row_label": label,
                        "seats": [
                            {
                                "number": seat.number,
                                "x": seat.x,
                                "y": seat.y,
                                "accessible": seat.is_accessible,
                            }
                            for seat in sorted(rows_by_label[label], key=lambda s: s.x)
                        ],
                    }
                    for label in row_order
                ],
            }
        )
    return {"chart": {"name": chart.name}, "sections": sections}


def import_chart_data(venue, data, *, name=None, replace=False):
    """Create (or, with `replace=True`, overwrite) a SeatingChart on `venue`
    from `data` (the shape produced by `export_chart_data`). The chart's
    organization is always `venue.organization` -- never taken from the
    JSON, so an imported file can never plant rows in another tenant.

    - No chart with this name exists yet on the venue: create it fresh.
    - One already exists and `replace` is False: raises ChartImportError
      (nothing is touched).
    - One already exists and `replace` is True: refuses (ChartImportError,
      nothing touched) if any of its current seats back a live (non-void)
      Ticket; otherwise its sections (and their seats, via cascade) are
      deleted and rebuilt from `data` in their place, so its pk/venue/name
      identity survives the import.

    Returns the SeatingChart.
    """
    from orders.models import Ticket  # local import: orders imports venues, not vice versa

    chart_name = name or data["chart"]["name"]
    organization = venue.organization

    existing = SeatingChart.objects.filter(
        organization=organization, venue=venue, name=chart_name
    ).first()
    if existing is not None:
        if not replace:
            raise ChartImportError(
                f"A seating chart named {chart_name!r} already exists on {venue} "
                f"(id={existing.pk}). Pass replace=True to overwrite it."
            )
        live_ticket_seats = (
            Ticket.objects.filter(seat__section__chart=existing)
            .exclude(status=Ticket.Status.VOID)
            .count()
        )
        if live_ticket_seats:
            raise ChartImportError(
                f"Chart {chart_name!r} (id={existing.pk}) has {live_ticket_seats} seat(s) with "
                "a live ticket issued; refusing to overwrite it. Void/refund those tickets "
                "first, or import under a different name."
            )
        chart = existing
        chart.sections.all().delete()  # cascades to their Seats
    else:
        chart = SeatingChart.objects.create(organization=organization, venue=venue, name=chart_name)

    for section_data in data["sections"]:
        layout = section_data.get("layout", {})
        section = Section.objects.create(
            organization=organization,
            chart=chart,
            name=section_data["name"],
            ordering=section_data.get("ordering", 0),
            tier=section_data.get("tier", ""),
            numbering_scheme=section_data.get(
                "numbering_scheme", Section.NumberingScheme.SEQUENTIAL
            ),
            row_label_scheme=section_data.get(
                "row_label_scheme", Section.RowLabelScheme.SKIP_IO
            ),
            origin_x=layout.get("origin_x", 0.0),
            origin_y=layout.get("origin_y", 0.0),
            rotation=layout.get("rotation", 0.0),
            seat_pitch=layout.get("seat_pitch", 1.0),
            row_pitch=layout.get("row_pitch", 1.0),
            arc_radius=layout.get("arc_radius"),
            pivot_mode=layout.get("pivot_mode", Section.PivotMode.CENTER),
            pivot_x=layout.get("pivot_x", 0.0),
            pivot_y=layout.get("pivot_y", 0.0),
        )
        seats = [
            Seat(
                organization=organization,
                section=section,
                row_label=row["row_label"],
                number=str(seat_data["number"]),
                x=seat_data.get("x", 0.0),
                y=seat_data.get("y", 0.0),
                is_accessible=bool(seat_data.get("accessible", False)),
            )
            for row in section_data.get("rows", [])
            for seat_data in row.get("seats", [])
        ]
        Seat.objects.bulk_create(seats)

    return chart
