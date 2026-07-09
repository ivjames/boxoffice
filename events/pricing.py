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
"""

from .models import PriceTier


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
