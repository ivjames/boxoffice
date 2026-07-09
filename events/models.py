from django.db import models

from tenants.models import TenantScopedModel
from venues.models import Seat, SeatingChart, Section, Venue


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
    Tickets/Holds (RESERVED) — see docs/ARCHITECTURE.md "Seating".

    `seating_chart` (Phase A of the seating-chart epic, docs/SEATING.md)
    makes chart selection explicit instead of always implicitly resolving to
    the venue's first SeatingChart (the pre-Phase-A behavior, still used as
    the fallback when this is null -- see orders.services.get_seating_chart,
    the single place that resolution happens). Null is the common case for a
    venue with exactly one chart; set it explicitly once a venue has more
    than one chart in play (e.g. a "Cabaret setup" vs. "Standard house") so
    a given performance always uses the one it was actually built for."""

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
    seating_chart = models.ForeignKey(
        SeatingChart,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performances",
        help_text=(
            "Explicit chart choice for a RESERVED performance. Leave blank to use the venue's "
            "first seating chart (correct for the common one-chart-per-venue case)."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "event"]),
            models.Index(fields=["organization", "starts_at"]),
            models.Index(fields=["seating_chart"]),
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


# --- Phase C: visual pricing zones (seating-chart epic, docs/SEATING.md) --
#
# Two-layer shape, per the epic's locked "reusable templates vs. per-
# performance instances" decision:
#
# - `ZoneTemplate` is a reusable, org-scoped named/colored palette entry
#   (e.g. "Premium" / #c1121f) -- defined once, offered on every
#   performance's zone editor.
# - `PricingZone` is the actual per-performance zone: it snapshots
#   `name`/`color` from its template AT APPLY TIME (not a live FK read), and
#   carries its own `amount` (a price only ever makes sense for one
#   performance at a time). Editing a ZoneTemplate later, or a different
#   performance's PricingZone, never mutates an already-applied zone --
#   there is no live reference back to the template for display purposes,
#   only `template` (nullable, SET_NULL) kept around for provenance/"clone
#   from this template again" convenience. See events/pricing.py for how a
#   zone wins over a PriceTier when resolving a reserved seat's price, and
#   events/zones.py for the CRUD/clone service functions that create and
#   mutate these.


class ZoneTemplate(TenantScopedModel):
    """A reusable, org-scoped named/colored pricing-zone palette entry
    (e.g. "Premium"/#c1121f, "Standard"/#1d4ed8). Define once, apply/clone
    onto any performance via events.zones.apply_zone -- this row itself
    carries no price; the price is set per-performance on the PricingZone
    instance created from it."""

    name = models.CharField(max_length=255)
    color = models.CharField(max_length=7, default="#2563eb", help_text="Hex color, e.g. #2563eb.")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="unique_zone_template_name_per_org"
            ),
        ]
        ordering = ["organization", "name"]

    def __str__(self):
        return self.name


class PricingZone(TenantScopedModel):
    """A named, colored, priced group of seats on ONE Performance -- the
    per-performance INSTANCE created by applying a ZoneTemplate (or an
    ad-hoc name/color typed on the fly). `name`/`color` are snapshotted at
    apply time (see events.zones.apply_zone), never read live off
    `template`, so a later template edit -- or a zone edit on some other
    performance -- can never retroactively change this row. `amount` is
    this zone's price for this performance only.

    A seat belongs to at most one zone per performance -- enforced in
    events.zones' service functions (which route every seat/zone
    assignment through a single "remove from any other zone on this
    performance, then add" step), not at the DB level, since the M2M
    through model (PricingZoneSeat) can't express "unique per performance"
    directly (a seat legitimately belongs to different zones across
    different performances)."""

    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="pricing_zones")
    template = models.ForeignKey(
        ZoneTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="zones",
        help_text="The template this zone was applied/cloned from, if any -- provenance only.",
    )
    name = models.CharField(max_length=255)
    color = models.CharField(max_length=7)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    seats = models.ManyToManyField(
        Seat, through="PricingZoneSeat", related_name="pricing_zones", blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
        ]
        ordering = ["performance", "name"]

    def __str__(self):
        return f"{self.name} ({self.amount}) — {self.performance}"


class PricingZoneSeat(TenantScopedModel):
    """Through row for PricingZone.seats: one row per seat assigned to a
    zone for that zone's one Performance. See PricingZone's docstring for
    the "at most one zone per performance" rule this participates in."""

    zone = models.ForeignKey(PricingZone, on_delete=models.CASCADE, related_name="zone_seats")
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE, related_name="zone_seats")

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["zone"]),
            models.Index(fields=["seat"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["zone", "seat"], name="unique_seat_per_zone"),
        ]

    def __str__(self):
        return f"{self.seat} in {self.zone}"
