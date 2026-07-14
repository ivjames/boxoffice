from django.db import models

from tenants.models import TenantScopedModel


class PromoCode(TenantScopedModel):
    """A discount code a buyer types at checkout to knock a percentage or a
    flat amount off their cart.

    v1 SCOPING DECISION -- org-wide only. A PromoCode belongs to one theater
    (via TenantScopedModel.organization) and applies to ANY performance that
    theater sells; there is deliberately no per-event / per-performance
    restriction yet. That was a conscious "ship the money path first" call,
    not an oversight, and it's cheap to widen later precisely BECAUSE every
    usability decision is funneled through one place: promotions.services.
    validate_code (the hold-agnostic core every apply path routes through --
    orders.services.apply_promo_code calls it). Adding per-event scoping later
    is one nullable FK on this model (e.g. `event = FK(events.Event, null=True)`)
    plus ONE extra clause in that validator ("if promo.event_id and
    promo.event_id != hold's event -> reject")
    -- no money-path, snapshot, or fulfillment code has to change, because none
    of them re-derive usability; they trust the snapshot the validator already
    approved (Hold.promo_code / discount_amount).

    Redemption accounting is intentionally split: `redemption_count` is bumped
    ONLY at fulfillment (payments.services.fulfill_hold ->
    promotions.services.record_redemption), never at apply-to-cart time, so an
    abandoned cart doesn't burn a code's remaining uses. The cap is therefore a
    SOFT cap -- see validate_code / fulfill_hold for the deliberate stance that
    a paid order is never rejected for promo state, accepting a rare
    over-cap-by-one under concurrency rather than ever refusing money already
    collected.

    Codes are never hard-deleted once redeemed (an Order snapshots
    `promo_code_text`, but keeping the row preserves the audit/report trail);
    `is_active` doubles as the archive flag -- flip it False to retire a code.
    """

    class Kind(models.TextChoices):
        PERCENT = "percent", "Percentage off"
        FIXED = "fixed", "Fixed amount off"

    # Stored normalized (see save()) so the plain UniqueConstraint below
    # enforces CASE-INSENSITIVE uniqueness identically on SQLite and Postgres,
    # without a functional/expression index (which the two backends spell
    # differently). Lookups (promotions.services.get_usable_code) normalize the
    # same way before querying, so "summer", "SUMMER", and " Summer " are one
    # code.
    code = models.CharField(max_length=32)

    kind = models.CharField(max_length=16, choices=Kind.choices)
    # PERCENT: 0-100 (a percentage). FIXED: a flat amount in MAJOR units (e.g.
    # dollars), in `currency`. The CheckConstraint below only guarantees
    # non-negative at the DB level; the 0-100 range for percent and any
    # business bounds are enforced in the service/validation layer and the
    # (out-of-scope-here) dashboard form.
    value = models.DecimalField(max_digits=10, decimal_places=2)

    # FIXED-amount codes only: the currency the `value` is denominated in. Blank
    # means "the org's own currency" (organization.currency) -- the common case
    # for a single-currency theater. validate_code rejects applying a code whose
    # explicit currency doesn't match the cart's charge currency (a code can't
    # discount a charge in a different currency). Meaningless for PERCENT (a
    # percentage is currency-agnostic), so left blank there.
    currency = models.CharField(max_length=3, blank=True)

    # Optional validity window. Null = open-ended on that end (no start / no
    # expiry). Checked in validate_code against `now`.
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    # Soft usage cap. Null = unlimited. Compared against redemption_count in
    # validate_code (`max_redemptions is not None and redemption_count >=
    # max_redemptions`).
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    # Bumped ONLY at fulfillment (see class docstring / record_redemption), so
    # it counts codes actually PAID with, not codes merely applied to a cart.
    redemption_count = models.PositiveIntegerField(default=0)

    # Minimum cart SUBTOTAL (gross, pre-discount, major units) required for the
    # code to apply. Null = no minimum. Compared against hold_total(hold) in
    # validate_code.
    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Doubles as the archive flag (see class docstring): a redeemed code is
    # retired by flipping this False, never deleted.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            # The hot path is "find this org's code by its (normalized) text"
            # -- get_usable_code filters (organization, code).
            models.Index(fields=["organization", "code"]),
        ]
        constraints = [
            # One code string per org. `code` is stored normalized (strip/
            # upper) so this plain unique constraint gives case-insensitive
            # uniqueness with identical semantics on SQLite and Postgres -- no
            # functional index needed. See the `code` field comment.
            models.UniqueConstraint(
                fields=["organization", "code"], name="unique_promo_code_per_org"
            ),
            # Belt-and-braces DB floor: a discount value can never be negative,
            # regardless of what any form or import path tries to write. The
            # percent 0-100 ceiling and other bounds are validated above this
            # in the service layer.
            models.CheckConstraint(condition=models.Q(value__gte=0), name="promo_value_nonnegative"),
        ]

    def save(self, *args, **kwargs):
        """Normalize `code` to stripped-uppercase on every write so the
        UniqueConstraint (and get_usable_code's matching lookup) treat codes
        case-insensitively. Done in save() rather than a form clean so it holds
        for EVERY write path -- admin, dashboard form, data import, shell --
        not just the one form. `code` may be blank on a not-yet-populated
        instance; str() guards against a None slipping through."""
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} ({self.get_kind_display()})"
