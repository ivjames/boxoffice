"""Promo-code service layer: lookup, validation, discount math, and redemption
accounting. This is the ONE place usability of a code is decided, so widening
the model later (per-event scoping, per-buyer caps, ...) is a change confined
here plus the model -- see PromoCode's docstring.

DEPENDENCY DIRECTION (do not break): this module must NOT import from orders or
payments. The money path depends on promotions, not the other way around --
orders.services imports PromoError/get_usable_code/validate_code/
compute_discount/record_redemption from here, and payments.services reaches
promotions only through orders. Keeping validation hold-AGNOSTIC (it takes a
subtotal + currency, never a Hold) is what lets it live here without importing
the caller's models, and is why apply_promo_code lives in orders.services (it's
the only piece that needs a Hold).
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F
from django.utils import timezone

from .models import PromoCode


class PromoError(Exception):
    """A promo code can't be applied. The message is SAFE to flash directly to
    the buyer (via the messages framework) -- every raise site below phrases it
    for a buyer's eyes, never leaking code internals or another tenant's data.

    Defined here (not in orders) so promotions stays free of any orders import;
    orders.services re-exports it so views can catch PromoError alongside
    HoldError without reaching across into this app. It is deliberately NOT a
    subclass of orders.HoldError -- subclassing that would force this module to
    import orders and invert the dependency (see the module docstring)."""


def get_usable_code(organization, code, *, for_update=False):
    """The org's PromoCode matching `code` (normalized strip/upper, mirroring
    PromoCode.save()), or None if there's no such row. ALWAYS filtered to
    `organization` -- a code lookup can never cross a tenant boundary, per the
    "tenant isolation is non-negotiable" rule.

    This only finds the row; it does NOT judge whether the code is usable right
    now (active/window/cap/minimum) -- that's validate_code's job, kept separate
    so the caller can lock the row (for_update) and validate inside one
    transaction. `for_update` adds select_for_update() so apply_promo_code can
    hold the row while it snapshots the discount onto the Hold, serializing
    concurrent applies of the same code on Postgres (a no-op on SQLite, whose
    IMMEDIATE-mode whole-DB write lock already serializes -- same parity note as
    orders.services)."""
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    qs = PromoCode.objects.filter(organization=organization, code=normalized)
    if for_update:
        qs = qs.select_for_update()
    return qs.first()


def validate_code(promo, *, subtotal, currency, now=None):
    """Raise PromoError (buyer-safe message) if `promo` can't be applied to a
    cart whose gross subtotal is `subtotal` in `currency`; return None if it's
    fine. Hold-AGNOSTIC on purpose -- it takes the two facts it needs
    (subtotal + charge currency) rather than a Hold, so it needs no orders
    import and can live in this app (see module docstring).

    Checks, in order:
      - inactive / archived (is_active False);
      - before its start window (starts_at in the future);
      - after its end window (ends_at in the past);
      - maxed out (SOFT cap: max_redemptions set and redemption_count already
        at/over it -- redemption_count only counts PAID redemptions, so this
        rejects at apply time before a buyer wastes effort, while fulfillment
        deliberately never re-rejects -- see fulfill_hold);
      - cart under min_order_amount;
      - a FIXED code whose explicit currency doesn't match the charge currency
        (a code denominated in one currency can't discount a charge in another;
        a blank promo.currency means "the org's currency" and is not checked
        here -- the caller resolves the charge currency to exactly that);
      - a discount that would zero out or exceed the cart. Stripe rejects a $0
        Checkout (and a negative one is nonsense), and comps are a separate
        staff flow -- so a code that swallows the whole cart is refused rather
        than silently clamped to a free order.
    """
    now = now or timezone.now()

    if not promo.is_active:
        raise PromoError("That code isn't valid.")
    if promo.starts_at is not None and now < promo.starts_at:
        raise PromoError("That code isn't active yet.")
    if promo.ends_at is not None and now > promo.ends_at:
        raise PromoError("That code has expired.")
    if promo.max_redemptions is not None and promo.redemption_count >= promo.max_redemptions:
        raise PromoError("That code has reached its redemption limit.")
    if promo.min_order_amount is not None and subtotal < promo.min_order_amount:
        raise PromoError(
            f"That code requires an order of at least {promo.min_order_amount} "
            f"{(currency or '').upper()}.".strip()
        )
    if (
        promo.kind == PromoCode.Kind.FIXED
        and promo.currency
        and promo.currency.upper() != (currency or "").upper()
    ):
        raise PromoError("That code can't be used for this order's currency.")
    if compute_discount(promo, subtotal) >= subtotal:
        # A percent >=100 or a fixed amount >= the cart would leave $0 (or less)
        # to charge. Stripe won't create a $0 Checkout Session, so we refuse the
        # code rather than mint a comp -- comps are an intentional, separate
        # staff flow, not something a public code should trigger.
        raise PromoError("This code can't be applied to this order.")


def compute_discount(promo, subtotal):
    """The discount amount (major units, Decimal, quantized to cents, never
    negative) `promo` takes off a gross `subtotal`.

    PERCENT: `subtotal * value / 100`, rounded half-up to cents. FIXED:
    `min(value, subtotal)` -- a flat code never discounts more than the cart
    itself (so the raw math can't go negative), also quantized to cents.

    Pure math with NO usability judgment -- validate_code owns "may this apply
    at all"; this owns "by how much". Both fulfillment and the Stripe coupon
    (payments.services) reconcile exactly against this value because it's the
    single source of the discount, snapshotted onto the Hold at apply time (see
    orders.services.apply_promo_code / Hold.discount_amount)."""
    cents = Decimal("0.01")
    if promo.kind == PromoCode.Kind.PERCENT:
        discount = (subtotal * promo.value / Decimal(100)).quantize(cents, rounding=ROUND_HALF_UP)
    else:
        discount = min(promo.value, subtotal).quantize(cents, rounding=ROUND_HALF_UP)
    if discount < Decimal("0.00"):
        return Decimal("0.00")
    return discount


def record_redemption(promo):
    """Atomically bump `promo`'s redemption_count by one, at the DB level via an
    F() expression on a pk-scoped queryset update -- so it composes inside the
    caller's transaction (payments.services.fulfill_hold, already
    @transaction.atomic) and can't lose an increment to a read-modify-write
    race between two concurrent fulfillments. Does NOT refresh the in-memory
    `promo` instance; callers that need the new count re-read it.

    Called ONLY at fulfillment, never at apply-to-cart time (see PromoCode's
    docstring) -- this counts codes actually paid with."""
    PromoCode.objects.filter(pk=promo.pk).update(
        redemption_count=F("redemption_count") + 1
    )
