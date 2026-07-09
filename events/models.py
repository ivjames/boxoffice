from django.db import models

from tenants.models import TenantScopedModel
from venues.models import Section, Venue


class Event(TenantScopedModel):
    """A production/show (e.g. "A Christmas Carol"). One Event can have many
    dated Performances."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="event_images/", blank=True, null=True)
    category = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="unique_event_slug_per_org"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class Performance(TenantScopedModel):
    """A single dated/timed showing of an Event at a Venue. `seating_mode`
    decides whether availability is tracked via GAAllocation (GA) or per-Seat
    Tickets/Holds (RESERVED) — see docs/ARCHITECTURE.md "Seating"."""

    class SeatingMode(models.TextChoices):
        GA = "GA", "General admission"
        RESERVED = "RESERVED", "Reserved seating"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        CANCELLED = "cancelled", "Cancelled"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="performances")
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="performances")
    starts_at = models.DateTimeField()
    seating_mode = models.CharField(max_length=10, choices=SeatingMode.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "event"]),
            models.Index(fields=["organization", "starts_at"]),
        ]
        ordering = ["starts_at"]

    def __str__(self):
        return f"{self.event.title} — {self.starts_at:%Y-%m-%d %H:%M}"


class PriceTier(TenantScopedModel):
    """A price. At least one of `performance` / `section` must be set (both
    null is forbidden); see events/pricing.py for the resolution rule this
    shape supports:

    - `performance` set, `section` null: a flat price for a GA performance.
    - `section` set, `performance` null: the DEFAULT price for seats in that
      Section, applied on every reserved-seating performance that uses its
      chart (unless overridden -- see below).
    - `performance` AND `section` both set: a per-performance OVERRIDE price
      for that Section on that one Performance only (e.g. a higher evening
      price), taking precedence over the section's chart-wide default.

    Reserved-seat pricing should never be read directly off this model --
    use `events.pricing.resolve_seat_tier(performance, section)`, which
    implements the override-then-default lookup above.
    """

    name = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")

    performance = models.ForeignKey(
        Performance,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="price_tiers",
        help_text="Set for a GA performance's flat price tier.",
    )
    section = models.ForeignKey(
        Section,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="price_tiers",
        help_text="Set for a reserved-seating price tier scoped to a Section.",
    )

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["organization", "section"]),
        ]
        constraints = [
            # At least one of performance/section must be set. Both set is
            # now allowed (a per-performance section override -- see the
            # class docstring); both null is still forbidden.
            models.CheckConstraint(
                condition=(
                    models.Q(performance__isnull=False) | models.Q(section__isnull=False)
                ),
                name="price_tier_requires_performance_or_section",
            ),
        ]
        ordering = ["organization", "name"]

    def __str__(self):
        return f"{self.name} ({self.amount} {self.currency})"


class GAAllocation(TenantScopedModel):
    """Capacity tracking for a single GA Performance. `sold` is a running
    count maintained by Phase 3/4 booking logic; not touched here."""

    performance = models.OneToOneField(
        Performance, on_delete=models.CASCADE, related_name="ga_allocation"
    )
    capacity = models.PositiveIntegerField()
    sold = models.PositiveIntegerField(default=0)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(sold__lte=models.F("capacity")),
                name="ga_sold_lte_capacity",
            ),
        ]

    def __str__(self):
        return f"{self.performance} — {self.sold}/{self.capacity}"
