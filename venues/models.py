from django.db import models

from tenants.models import TenantScopedModel


class Venue(TenantScopedModel):
    """A physical location a theater performs at. Most tenants have exactly
    one, but the model allows for touring/second-space orgs."""

    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    timezone = models.CharField(max_length=63, default="UTC")

    class Meta(TenantScopedModel.Meta):
        ordering = ["name"]

    def __str__(self):
        return self.name


class SeatingChart(TenantScopedModel):
    """A named seat layout at a Venue (e.g. "Main house", "Cabaret setup").
    A Venue can have more than one chart; a Performance points at whichever
    chart is in use via its Venue relationship at booking time (Phase 3)."""

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="seating_charts")
    name = models.CharField(max_length=255)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [models.Index(fields=["venue"])]
        constraints = [
            models.UniqueConstraint(fields=["venue", "name"], name="unique_chart_name_per_venue"),
        ]
        ordering = ["venue", "name"]

    def __str__(self):
        return f"{self.name} ({self.venue})"


class Section(TenantScopedModel):
    """A group of seats within a SeatingChart (e.g. "Orchestra", "Balcony").
    `ordering` controls display order (front-of-house to back), not DB
    insertion order.

    Phase A (seating-chart epic, docs/SEATING.md) adds the layout params
    that DRIVE seat generation (venues.generation.generate_seats): origin/
    pitch/rotation/arc_radius describe where seats in this section go,
    numbering_scheme/row_label_scheme describe what they're called.

    docs/EDITOR.md's live rework changes the authoring model: the dashboard
    chart editor (static/js/chart_editor.js + static/js/seat_geometry.js)
    renders this section's seats LIVE, client-side, straight from these
    params -- no server round-trip, no persisted Seat needed to preview a
    change. `Seat.x/y` (and the rest of the Seat table for this section) is
    just the last-saved snapshot: Save recomputes and persists it from
    these exact params via venues.generation.generate_seats, which is the
    same formula seat_geometry.js mirrors (see that file's header and
    generation.py's module docstring for the "one place" contract). Per-
    seat deletions/ADA flags are tracked here (removed_seats/
    accessible_seats, by (row_label, number) identity, NOT by Seat pk --
    pks don't survive a regenerate) so they persist across live param
    changes and are re-applied every Save.
    """

    class NumberingScheme(models.TextChoices):
        SEQUENTIAL = "sequential", "Sequential (1, 2, 3…)"
        ODD_DESC_LEFT = "odd_desc_left", "Odd, descending toward the aisle (…5, 3, 1)"
        EVEN_ASC_RIGHT = "even_asc_right", "Even, ascending away from the aisle (2, 4, 6…)"
        HUNDREDS = "hundreds", "Hundreds by row (101, 102… / 201, 202…)"

    class RowLabelScheme(models.TextChoices):
        SKIP_IO = "skip_io", "A–Z skipping I/O, then AA, BB…"
        ALL_LETTERS = "all_letters", "A–Z including I/O, then AA, BB…"

    class OffsetMode(models.TextChoices):
        REPEATED = "repeated", "Repeated (constant shift every row)"
        ALTERNATING = "alternating", "Alternating (stagger every other row)"

    chart = models.ForeignKey(SeatingChart, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=255)
    ordering = models.PositiveIntegerField(default=0)
    tier = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Optional grouping label, e.g. Orchestra/Parterre/Balcony. Free text in Phase A; "
            "Phase C's visual pricing zones may key off it for defaults/labeling."
        ),
    )

    # -- layout params (drive venues.generation.generate_seats) -----------
    origin_x = models.FloatField(default=0.0, help_text="X of this section's front-left corner.")
    origin_y = models.FloatField(default=0.0, help_text="Y of this section's front-left corner.")
    rotation = models.FloatField(
        default=0.0,
        help_text=(
            "Degrees clockwise the section's seat grid is rotated around its origin. Phase A "
            "applies this as a simple whole-grid rotation during generation; per-row raked "
            "offsets are Phase B."
        ),
    )
    seat_pitch = models.FloatField(default=1.0, help_text="Spacing between adjacent seats in a row.")
    row_pitch = models.FloatField(default=1.0, help_text="Spacing between rows.")
    row_x_offset = models.FloatField(
        default=0.0,
        help_text=(
            "Offset amount in local x units. Meaning depends on offset_mode: REPEATED applies "
            "it every row, growing with row_index (0.5 shifts row B 0.5 right of row A, row C "
            "1.0, and so on -- turns a raked/diagonal side section into a trapezoid). "
            "ALTERNATING applies the same constant amount to every OTHER row only (brick/"
            "stadium stagger), row_index 1, 3, 5… -- see venues.generation._row_x_offset, "
            "mirrored in static/js/seat_geometry.js. 0.0 (default) reproduces a plain grid."
        ),
    )
    offset_mode = models.CharField(
        max_length=20,
        choices=OffsetMode.choices,
        default=OffsetMode.REPEATED,
        help_text="How row_x_offset is applied across rows -- see its help text.",
    )
    alt_row_seat_delta = models.IntegerField(
        default=0,
        help_text=(
            "ALTERNATING offset_mode only: seats added (positive) or dropped (negative) on "
            "every other row (row_index 1, 3, 5…), on top of seats_per_row -- e.g. +1 for a "
            "brick-stagger row that's one seat longer, -1 for one shorter. Each row is floored "
            "at 1 seat. Ignored in REPEATED mode."
        ),
    )
    arc_radius = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Curvature radius for a fanned/curved section, e.g. a center orchestra block. When "
            "set, seats in a row are placed along a circular arc of this radius instead of a "
            "straight line -- the arc curves the rows IN PLACE (the section's front-row-center "
            "seat always sits at the section's origin regardless of arc_radius; the radius only "
            "controls how tightly the rows bow, not where the section sits) -- see "
            "venues.generation._fanned_local, mirrored in static/js/seat_geometry.js. "
            "Null/blank (default) means straight (grid or raked, per rotation/row_x_offset)."
        ),
    )

    # -- live-editor shape (drive venues.generation.compute_row_counts) ---
    rows = models.PositiveIntegerField(
        default=4, help_text="Live editor shape: number of rows to generate."
    )
    seats_per_row = models.PositiveIntegerField(
        default=8,
        help_text=(
            "Live editor shape: base seats per row. ALTERNATING offset_mode adds "
            "alt_row_seat_delta to this on every other row."
        ),
    )

    # -- per-seat overrides (survive regeneration, see venues.generation) -
    removed_seats = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "[[row_label, number], ...] seat identities deleted via the editor's seat popover. "
            "Re-applied on every regenerate so a deleted seat (e.g. an aisle gap) stays gone "
            "even after live param changes."
        ),
    )
    accessible_seats = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "[[row_label, number], ...] seat identities flagged accessible via the editor's "
            "seat popover. Re-applied on every regenerate, same as removed_seats."
        ),
    )

    # -- authoring metadata (drive venues.generation.generate_seats) ------
    numbering_scheme = models.CharField(
        max_length=20, choices=NumberingScheme.choices, default=NumberingScheme.SEQUENTIAL
    )
    row_label_scheme = models.CharField(
        max_length=20, choices=RowLabelScheme.choices, default=RowLabelScheme.SKIP_IO
    )

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [models.Index(fields=["chart"])]
        constraints = [
            models.UniqueConstraint(fields=["chart", "name"], name="unique_section_name_per_chart"),
        ]
        ordering = ["chart", "ordering", "name"]

    def __str__(self):
        return f"{self.name}"


class Seat(TenantScopedModel):
    """A single bookable seat within a Section. `x`/`y` are normalized
    coordinates for rendering the interactive seat map (Phase 3 storefront);
    units/scale are up to the map renderer."""

    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="seats")
    row_label = models.CharField(max_length=10)
    number = models.CharField(max_length=10)
    x = models.FloatField(default=0)
    y = models.FloatField(default=0)
    is_accessible = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [models.Index(fields=["section"])]
        constraints = [
            models.UniqueConstraint(
                fields=["section", "row_label", "number"], name="unique_seat_per_section"
            ),
        ]
        ordering = ["section", "row_label", "number"]

    def __str__(self):
        return f"{self.row_label}{self.number}"
