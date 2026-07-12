import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from events.models import Performance, PriceTier, PricingZone
from tenants.models import TenantScopedModel
from venues.models import Seat


def default_hold_expiry():
    """Named function (not a lambda) so migrations can serialize this default.
    Business logic for extending/refreshing holds lives in Phase 3."""
    return timezone.now() + timedelta(minutes=10)


# Alphabet for hand-typed ticket codes: A-Z0-9 minus every glyph that's a
# look-alike for another in common fonts, so a staffer reading a token off a
# ticket can't transpose one character for another regardless of the font.
# Dropped pairs: 0/O, 1/I/L, 2/Z, 5/S, 6/G, 8/B, U/V (kept one of each; O and
# 0, I/L and 1 are dropped outright). 26 chars -> ~4.7 bits each; all uppercase
# alphanumeric, so a code stays in QR "alphanumeric mode" and is <slug>-safe.
_TOKEN_ALPHABET = "ACDEFGHJKMNPQRTVWXY2345789"
_TOKEN_LENGTH = 10


def new_token():
    """Short, unguessable public token for Orders and Tickets.

    10 chars from _TOKEN_ALPHABET (~47 bits of entropy) instead of a UUID's 36.
    Kept short on purpose for two reasons: a Ticket's token both rides inside
    its QR code (as "<token>.<sig>" -- orders/tokens.scan_code) AND is what a
    staffer keys in by hand on the scanner's manual-entry fallback, so every
    character saved is a smaller QR and less to type. The alphabet omits
    look-alike characters (see _TOKEN_ALPHABET) so a hand-typed code can't be
    misread; only the token needs this -- the signature is machine-read only.

    ~47 bits is ample here. Unguessability is only ever ONE of three gates on
    redemption -- the per-ticket HMAC signature (orders/tokens.py) and the
    scanner-role login are the others -- so the token alone was never enough
    to redeem. And it keeps token COLLISIONS negligible at any realistic
    box-office volume (past a million-plus tickets); in the astronomically
    rare event two collide, the unique constraint rejects the insert and the
    Stripe path simply regenerates on webhook redelivery.

    Named (not a lambda) so migrations can serialize it as a field default,
    matching default_hold_expiry above. The alphabet is a subset of Django's
    `slug` URL converter's, so the redeem/confirmation routes match on
    <slug:token> without a custom converter."""
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


class Hold(TenantScopedModel):
    """A temporary reservation of inventory while a buyer is mid-checkout.
    GA holds set `price_tier` + `quantity`; reserved-seat holds attach Seats
    via the HoldSeat through model instead. Expiry/availability math is
    Phase 3 (storefront) — this app only carries the shape of the data.
    """

    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="holds")
    session_key = models.CharField(max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="holds",
    )
    expires_at = models.DateTimeField(default=default_hold_expiry)

    # GA selection.
    price_tier = models.ForeignKey(
        PriceTier, on_delete=models.CASCADE, null=True, blank=True, related_name="holds"
    )
    quantity = models.PositiveIntegerField(null=True, blank=True)
    # Snapshot of the GA tier price at hold-creation time -- the GA analogue
    # of HoldSeat.unit_amount for reserved seats. Frozen here so hold_total(),
    # the Stripe line item, and _fulfill_ga all charge/record the price the
    # buyer saw, immune to a PriceTier.amount edit made between hold creation
    # and payment. Nullable only so it can be added without a data migration;
    # set_ga_hold always populates it on new GA holds, and the GA money paths
    # fall back to the live tier amount when it's null (a hold in flight
    # across the deploy that added this column). See orders.services.
    ga_unit_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    # Reserved-seat selection.
    seats = models.ManyToManyField(Seat, through="HoldSeat", related_name="holds", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["performance", "expires_at"]),
            models.Index(fields=["session_key"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(quantity__isnull=True, price_tier__isnull=True)
                    | models.Q(quantity__isnull=False, price_tier__isnull=False)
                ),
                name="hold_ga_fields_together",
            ),
        ]

    def __str__(self):
        return f"Hold #{self.pk} for {self.performance}"


class HoldSeat(TenantScopedModel):
    """Through row for a reserved-seat Hold: one row per held Seat.

    Phase C (seating-chart epic, docs/SEATING.md) money-path note: a seat's
    price can now come from a `PricingZone` instead of a `PriceTier` (see
    events.pricing.resolve_seat_price), and zones don't have a PriceTier at
    all -- so `price_tier` is now nullable, `pricing_zone` is added
    (nullable, SET_NULL) alongside it, and `unit_amount` SNAPSHOTS the
    resolved price at hold-creation time (orders.services.set_reserved_hold)
    instead of being read live off whichever of the two FKs is set. That
    snapshot is what makes fulfillment (payments.services._fulfill_reserved)
    and hold_total() immune to a zone/template price edit -- or the zone
    being deleted outright -- happening after this hold was created but
    before it's paid. `price_tier`/`pricing_zone` are provenance only, set
    to whichever source `resolve_seat_price` actually used at hold-creation
    time -- there is deliberately NO check constraint requiring one of them
    to stay non-null, because deleting a PricingZone (allowed even with
    active holds -- see events.zones.delete_zone) SET_NULLs `pricing_zone`
    on every HoldSeat that referenced it, which would otherwise leave a
    perfectly valid, already-priced (via `unit_amount`) HoldSeat unable to
    satisfy such a constraint."""

    hold = models.ForeignKey(Hold, on_delete=models.CASCADE, related_name="hold_seats")
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE, related_name="hold_seats")
    price_tier = models.ForeignKey(
        PriceTier, on_delete=models.PROTECT, null=True, blank=True, related_name="hold_seats"
    )
    pricing_zone = models.ForeignKey(
        PricingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="hold_seats"
    )
    unit_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Snapshot of the resolved price at hold-creation time -- see class docstring.",
    )

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["hold"]),
            models.Index(fields=["seat"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["hold", "seat"], name="unique_seat_per_hold"),
        ]

    def __str__(self):
        return f"{self.seat} on hold #{self.hold_id}"

    @property
    def price_label(self):
        """Display label for whichever priced this seat -- the zone's name
        if one applied, else the PriceTier's name."""
        if self.pricing_zone_id:
            return self.pricing_zone.name
        if self.price_tier_id:
            return self.price_tier.name
        return ""


class PerformanceSeatBlock(TenantScopedModel):
    """A "house kill": a Seat pulled from sale for ONE Performance only
    (sightline obstruction, tech hold, VIP hold, etc.) without touching the
    Seat itself -- the same seat is unaffected on every other performance
    that uses the same chart. Phase A of the seating-chart epic
    (docs/SEATING.md); reserved-availability math (orders.services) treats a
    blocked seat exactly like a ticketed/held one -- see
    reserved_seat_states's docstring for the resulting state precedence."""

    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="seat_blocks")
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE, related_name="performance_blocks")
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["performance", "seat"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["performance", "seat"], name="unique_seat_block_per_performance"
            ),
        ]

    def __str__(self):
        return f"{self.seat} blocked on {self.performance}"


class Order(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    performance = models.ForeignKey(Performance, on_delete=models.PROTECT, related_name="orders")
    buyer_email = models.EmailField()
    buyer_name = models.CharField(max_length=255, blank=True)
    # The buyer's per-theater guest account, keyed off buyer_email at
    # fulfillment (payments.services.fulfill_hold -> guests.GuestAccount).
    # Nullable/SET_NULL: an order can exist without one (a Stripe session that
    # carried no email, or a comp), and deleting a guest must not delete their
    # order history -- buyer_email stays the authoritative contact on the row
    # regardless. This is what lets the guest portal list "all your orders".
    guest = models.ForeignKey(
        "guests.GuestAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Public-facing lookup token for the confirmation page (/tickets/<token>/).
    # Short code (see new_token) -- max_length leaves headroom for the 10-char
    # tokens plus any longer rows predating the switch to short tokens.
    token = models.CharField(max_length=36, default=new_token, unique=True, editable=False)

    # Phase 4 (Stripe checkout + webhooks): fields only, no logic here.
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["stripe_checkout_session_id"]),
        ]
        constraints = [
            # payments.services.fulfill_checkout_session()'s idempotency
            # relies on "does an Order already exist for this session id"
            # -- correct in sequence, but without a DB-level constraint two
            # truly concurrent webhook deliveries (both reading "no Order
            # yet" before either commits -- possible under Postgres's
            # default READ COMMITTED isolation; SQLite's harden_sqlite()
            # IMMEDIATE-mode whole-database lock is what prevents it today)
            # could each create their own Order/Ticket set, double-
            # fulfilling one payment. This constraint makes the DB itself
            # the backstop regardless of isolation level or backend; see
            # fulfill_checkout_session()'s IntegrityError handling for the
            # graceful fallback this enables. NULL/blank session ids
            # (pending/manually-created Orders) are excluded so they don't
            # collide with each other.
            models.UniqueConstraint(
                fields=["organization", "stripe_checkout_session_id"],
                condition=~models.Q(stripe_checkout_session_id__isnull=True)
                & ~models.Q(stripe_checkout_session_id=""),
                name="unique_stripe_checkout_session_per_org",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order {self.token} ({self.status})"


class OrderItem(TenantScopedModel):
    """Phase C note: `price_tier` is nullable -- a zone-priced reserved
    seat's OrderItem carries `pricing_zone` instead (whichever of the two
    priced it at fulfillment time, mirroring HoldSeat -- see its docstring,
    including why there's deliberately no check constraint requiring one of
    them to stay set: a zone can be deleted after an order that used it was
    already fulfilled, SET_NULLing `pricing_zone` here too). `unit_amount`
    is copied verbatim from the fulfilled HoldSeat's own snapshot
    (payments.services._fulfill_reserved), so an OrderItem's price is fixed
    the moment payment is fulfilled and immune to any later zone/template/
    tier edit -- or the zone/tier being deleted outright."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    price_tier = models.ForeignKey(
        PriceTier, on_delete=models.PROTECT, null=True, blank=True, related_name="order_items"
    )
    pricing_zone = models.ForeignKey(
        PricingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_items"
    )
    seat = models.ForeignKey(
        Seat, on_delete=models.PROTECT, null=True, blank=True, related_name="order_items"
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [models.Index(fields=["order"])]

    def __str__(self):
        if self.pricing_zone_id:
            label = self.pricing_zone.name
        elif self.price_tier_id:
            label = self.price_tier
        else:
            label = f"${self.unit_amount}"  # source since deleted -- unit_amount is still authoritative
        return f"{self.quantity} x {label} on order {self.order_id}"


class Ticket(TenantScopedModel):
    class Status(models.TextChoices):
        VALID = "valid", "Valid"
        USED = "used", "Used"
        VOID = "void", "Void"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="tickets")
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="tickets")
    seat = models.ForeignKey(
        Seat, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    holder_name = models.CharField(max_length=255, blank=True)
    # Rides inside the QR code AND is what staff key in on manual entry -- kept
    # short (see new_token) to shrink the QR and cut typing.
    token = models.CharField(max_length=36, default=new_token, unique=True, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.VALID)
    used_at = models.DateTimeField(null=True, blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scanned_tickets",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["performance", "seat"]),
        ]
        constraints = [
            # A seat can back at most one live (valid/used) ticket per
            # performance at a time; void tickets don't count so a
            # cancel-and-reissue doesn't collide with itself.
            models.UniqueConstraint(
                fields=["performance", "seat"],
                condition=models.Q(seat__isnull=False) & ~models.Q(status="void"),
                name="unique_live_ticket_per_performance_seat",
            ),
        ]

    def __str__(self):
        return f"Ticket {self.token} ({self.status})"


class Payment(TenantScopedModel):
    """Stub for Phase 4: fields only, no processing logic."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField(max_length=32, default="stripe")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=32, blank=True)
    provider_ref = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [models.Index(fields=["order"])]

    def __str__(self):
        return f"{self.provider} payment for order {self.order_id}"
