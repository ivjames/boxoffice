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
    numbering_scheme/row_label_scheme describe what they're called. Per the
    spec's "separate LOGICAL identity from VISUAL geometry" decision, these
    are authoring inputs only -- once generated, `Seat.x/y` is the
    authoritative, persisted, hand-editable-in-Phase-B position; changing a
    Section's layout params after the fact does NOT retroactively move
    existing seats (regenerate to apply new params -- see generate_seats's
    docstring for what that does/doesn't allow).
    """

    class NumberingScheme(models.TextChoices):
        SEQUENTIAL = "sequential", "Sequential (1, 2, 3…)"
        ODD_DESC_LEFT = "odd_desc_left", "Odd, descending toward the aisle (…5, 3, 1)"
        EVEN_ASC_RIGHT = "even_asc_right", "Even, ascending away from the aisle (2, 4, 6…)"
        HUNDREDS = "hundreds", "Hundreds by row (101, 102… / 201, 202…)"

    class RowLabelScheme(models.TextChoices):
        SKIP_IO = "skip_io", "A–Z skipping I/O, then AA, BB…"
        ALL_LETTERS = "all_letters", "A–Z including I/O, then AA, BB…"

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
    arc_radius = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Radius for a fanned/curved section. Not yet used for seat placement -- Phase A "
            "always generates a straight (optionally rotated) grid; reserved for Phase B's "
            "fanned-section geometry."
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
