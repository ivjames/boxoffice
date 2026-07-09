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
    insertion order."""

    chart = models.ForeignKey(SeatingChart, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=255)
    ordering = models.PositiveIntegerField(default=0)

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
