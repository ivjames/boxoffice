"""Central price-resolution for reserved-seat pricing. Every reserved-seat
code path (hold creation, the seat-map/performance_detail price display,
and payments fulfillment/line-items) must go through `resolve_seat_tier`
instead of querying PriceTier directly, so a per-performance override is
honored everywhere consistently. See events/models.py's PriceTier docstring
for the full rule this implements:

- `PriceTier(performance=P, section=None)`: a GA performance's flat price.
- `PriceTier(performance=None, section=S)`: the default/chart-wide price for
  Section S across every RESERVED performance that uses its chart.
- `PriceTier(performance=P, section=S)`: an override — the price for Section
  S specifically on performance P (e.g. a higher evening price), replacing
  the chart-wide default for that one performance only.
- `PriceTier(performance=None, section=None)`: still forbidden (the
  CheckConstraint on PriceTier rejects it at the DB level).

GA pricing (`resolve_ga_tier`) is unaffected by any of this -- a GA
performance's price tiers are still looked up directly by `performance`
with `section` null, exactly as before.

Phase C (seating-chart epic, docs/SEATING.md) adds `PricingZone` as a MORE
specific override that wins over everything above: `resolve_seat_price`
below is the entry point reserved-seat call sites should use going forward
-- it checks for a zone first, then falls back to `resolve_seat_tier`'s
override-then-default PriceTier rule. It returns a `ResolvedPrice` so
callers never need to branch on where the price came from.
"""

from .models import PriceTier, PricingZone


class PricingError(Exception):
    """No PriceTier resolves for the given performance/section (or
    performance). Message is safe to show directly to staff/buyers."""


def resolve_seat_tier(performance, section):
    """The PriceTier that applies to `section` on `performance`: the
    per-performance override if one exists, else the section's chart-wide
    default. Raises PricingError if neither exists (nothing has been priced
    yet for this section)."""
    override = PriceTier.objects.filter(
        organization=performance.organization_id, performance=performance, section=section
    ).first()
    if override is not None:
        return override

    default = PriceTier.objects.filter(
        organization=performance.organization_id, performance__isnull=True, section=section
    ).first()
    if default is not None:
        return default

    raise PricingError(
        f"No price tier is set for section {section.pk} ({section}) on performance "
        f"{performance.pk} (no override, no section default)."
    )


def resolve_ga_tier(performance):
    """The single flat PriceTier for a GA performance is looked up directly
    (a GA performance can have several named tiers -- e.g. "General
    admission" vs "Student" -- the caller picks which one; this helper just
    validates/returns one that legitimately belongs to this GA performance
    when only one tier is expected). Provided for symmetry with
    resolve_seat_tier; GA code paths that let the buyer choose among several
    tiers should keep querying `performance.price_tiers` directly."""
    tier = PriceTier.objects.filter(
        organization=performance.organization_id, performance=performance, section__isnull=True
    ).first()
    if tier is not None:
        return tier
    raise PricingError(f"No GA price tier is set for performance {performance.pk}.")


# --- Phase C: zone-aware resolution (docs/SEATING.md "C") ----------------


class ResolvedPrice:
    """The uniform result of `resolve_seat_price`: exposes `.amount` plus a
    display `.label`/`.color`, regardless of whether the price came from a
    `PricingZone` or a `PriceTier` -- so callers (hold creation, the
    storefront seat map, checkout line items) never need to branch on the
    source. `zone`/`tier` carry whichever underlying row actually applied
    (exactly one is set) for callers that need provenance, e.g. snapshotting
    `HoldSeat.pricing_zone`/`HoldSeat.price_tier` at hold time."""

    def __init__(self, *, amount, label, color=None, zone=None, tier=None):
        self.amount = amount
        self.label = label
        self.color = color
        self.zone = zone
        self.tier = tier

    @property
    def is_zone(self):
        return self.zone is not None

    def __repr__(self):
        source = "zone" if self.is_zone else "tier"
        return f"ResolvedPrice(amount={self.amount!r}, label={self.label!r}, source={source!r})"


def resolve_seat_price(performance, seat):
    """The effective price for `seat` on `performance`, in priority order:

    1. A `PricingZone` for `performance` whose seats include `seat` (Phase
       C's visual pricing zones -- most specific, always server-side).
    2. Else `resolve_seat_tier(performance, seat.section)` (the existing
       override-then-section-default `PriceTier` rule, unchanged).

    Returns a `ResolvedPrice`. Raises `PricingError` if neither a zone nor a
    tier resolves (nothing has been priced yet for this seat)."""
    zone = (
        PricingZone.objects.filter(
            organization=performance.organization_id, performance=performance, seats=seat
        )
        .first()
    )
    if zone is not None:
        return ResolvedPrice(amount=zone.amount, label=zone.name, color=zone.color, zone=zone)
    tier = resolve_seat_tier(performance, seat.section)
    return ResolvedPrice(amount=tier.amount, label=tier.name, color=None, tier=tier)


def zones_by_seat_id(performance):
    """{seat_id: PricingZone} for every seat currently assigned to a zone on
    `performance`. The bulk form of `resolve_seat_price`'s zone lookup, for
    call sites that resolve a whole chart's worth of seats at once instead
    of one at a time -- see orders.services.resolve_reserved_prices, which
    combines this with the existing `price_tiers_by_section` helper to
    implement the full zone-then-tier rule in bulk (one zone query + one
    PriceTier resolution per section, not per seat)."""
    result = {}
    zones = PricingZone.objects.filter(
        organization=performance.organization_id, performance=performance
    ).prefetch_related("seats")
    for zone in zones:
        for seat in zone.seats.all():
            result[seat.id] = zone
    return result
