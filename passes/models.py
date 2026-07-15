"""Season & flex passes (Phase 3): the entitlement model that lets a theater
sell an admission bundle once and redeem it against tickets later, without a
per-redemption charge.

TWO PASS KINDS, ONE PRODUCT TABLE (PassProduct.kind):

- SEASON: one admission per COVERED EVENT. The buyer picks WHICH performance
  of each covered event they attend, but gets exactly one admission per event
  (quantity-1-per-event). A four-show season subscription is one PassProduct
  whose `events` M2M names those four events; the holder redeems it once
  against a performance of each. "Already used this pass for this show" is the
  season backstop -- enforced both in the redemption service
  (fulfill_hold_with_pass) and by a DB partial-unique constraint
  (unique_season_event_redemption) so a true race can't double-admit one event.

- FLEX: N FUNGIBLE credits. 1 credit = 1 admission to ANY covered performance,
  spend them however the holder likes (four credits could be four seats to one
  show, or one seat to four shows). `credit_count` is the bundle size;
  PassPurchase.credits_remaining is the live balance, decremented at redemption
  and floored at 0 (status -> EXHAUSTED). Season passes carry no credit_count.

THE SNAPSHOT RULE (same sacred pattern as Hold.ga_unit_amount / OrderItem.
unit_amount): everything a PassPurchase needs to be redeemed -- its kind, its
credit count, its valid window, the SET of events it covers -- is FROZEN onto
the purchase row at purchase time (see PassPurchase). A later edit to the
PassProduct (renaming it, changing its price, adding/removing a covered event,
narrowing its valid window, archiving it) can never change what an
already-sold pass entitles its holder to. The product row is the sales
template; the purchase row is the contract.

DELETING A REDEMPTION FREES THE ENTITLEMENT: a redemption is undone by
DELETING its PassRedemption row (passes.services.restore_redemptions_for_order,
called from the money path when a redemption order is voided/refunded). For a
season pass, deleting the row frees the event slot (the partial-unique
constraint stops guarding that event). For a flex pass, the credit is restored
SEPARATELY -- restore sums the deleted rows' credits_used back onto
credits_remaining and flips EXHAUSTED -> ACTIVE -- because a flex credit is a
balance, not a slot.

DEPENDENCY DIRECTION (mirrors donations/promotions): this app is money-path-
AGNOSTIC. passes.models + passes.services must NOT import from orders or
payments. The money path (payments.services fulfill_pass_purchase /
fulfill_hold_with_pass) depends on passes, never the reverse -- which is why
the redemption/order linkage FKs below point OUT to orders by string label but
carry no behavior that reaches back into it.
"""

from django.db import models

from tenants.models import TenantScopedModel


class PassProduct(TenantScopedModel):
    """A pass a theater offers for sale -- the sales template a PassPurchase
    snapshots at purchase (see module docstring's snapshot rule). `kind`
    discriminates season vs flex; the credit/window/events fields shape what a
    purchase of it will entitle the holder to.

    `is_active` doubles as the storefront ENABLE flag AND the archive switch: a
    product with is_active=False is hidden from the storefront and can't be
    bought, but is never deleted (its `purchases` PROTECT the row, and past
    sales must keep their provenance). Retiring a pass is flipping this False.

    The `events` M2M is the crucial scoping knob and its EMPTY case is load-
    bearing: an empty `events` set means the pass covers ALL of the theater's
    events (an all-access pass); a non-empty set restricts it to exactly those
    events. This is snapshotted onto the purchase (PassPurchase.covered_events)
    so a later change to the product's coverage doesn't move the goalposts for
    an already-sold pass. `valid_from`/`valid_until` bound coverage in TIME
    (checked against performance.starts_at); either null = that side is open.
    """

    class Kind(models.TextChoices):
        SEASON = "season", "Season"
        FLEX = "flex", "Flex"

    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # FLEX ONLY: how many fungible admission credits a purchase grants. Null for
    # a season pass (which is one-per-event, not a credit balance). The
    # passproduct_credit_shape constraint below enforces this shape at the DB.
    credit_count = models.PositiveIntegerField(null=True, blank=True)
    # Time bounds on coverage, checked against performance.starts_at at
    # redemption (passes.services.pass_covers_performance). Either side null =
    # open on that side (no lower / no upper bound). Snapshotted onto the
    # purchase so a later product-window edit can't shrink a sold pass.
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    # Which events this pass covers. EMPTY = ALL events (see class docstring);
    # a non-empty set restricts coverage to exactly those events. Snapshotted
    # onto PassPurchase.covered_events at purchase.
    events = models.ManyToManyField(
        "events.Event", blank=True, related_name="pass_products"
    )
    # Storefront enable + archive switch (see class docstring). Never delete a
    # product; flip this False.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            # The hot lookup is "this org's active products" (the storefront
            # listing / get_active_products), keyed on (organization, is_active).
            models.Index(fields=["organization", "is_active"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price__gte=0),
                name="passproduct_price_nonnegative",
            ),
            # Enforce the kind/credit_count shape at the DB, not just in Python:
            # a FLEX product MUST have a positive credit_count (a zero-credit
            # flex pass is meaningless); a SEASON product MUST have none (it's
            # one-per-event, not a credit balance). Mirrors Hold's
            # hold_ga_fields_together shape constraint.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="flex",
                        credit_count__isnull=False,
                        credit_count__gt=0,
                    )
                    | models.Q(kind="season", credit_count__isnull=True)
                ),
                name="passproduct_credit_shape",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()}, {self.organization})"


class PassPurchase(TenantScopedModel):
    """A pass a buyer actually bought -- the redeemable CONTRACT, with every
    entitlement term FROZEN onto it at purchase (see the module docstring's
    snapshot rule). This is the row the redemption path locks, re-checks, and
    decrements; the PassProduct it came from is never re-read to decide what
    the holder is owed.

    ALL of `kind`, `credit_count`, `credits_remaining`, `valid_from`,
    `valid_until`, and `covered_events` are snapshots taken at
    payments.services.fulfill_pass_purchase time from the product. In
    particular:
      - `credits_remaining` starts equal to the product's credit_count (flex)
        and is the LIVE balance the redemption path decrements; null for a
        season pass.
      - `covered_events` is a copy of the product's `events` set AT PURCHASE.
        EMPTY = all events (same load-bearing empty case as PassProduct.events).

    `guest` (SET_NULL) links the pass to its per-theater buyer for the guest
    portal's "my passes" list; `order` (PROTECT) is the purchase order that
    paid for it (a kind=PASS OrderItem). status: ACTIVE until a flex pass runs
    out of credits (EXHAUSTED) or the whole purchase is refunded (REFUNDED);
    a season pass never goes EXHAUSTED (it has no credit balance -- its cap is
    one-per-event, enforced per event, not a running-out of a pool)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXHAUSTED = "exhausted", "Exhausted"
        REFUNDED = "refunded", "Refunded"

    product = models.ForeignKey(
        PassProduct, on_delete=models.PROTECT, related_name="purchases"
    )
    guest = models.ForeignKey(
        "guests.GuestAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="passes",
    )
    order = models.ForeignKey(
        "orders.Order", on_delete=models.PROTECT, related_name="pass_purchases"
    )

    # --- entitlement snapshots, all frozen at purchase (see class docstring) --
    kind = models.CharField(max_length=16, choices=PassProduct.Kind.choices)
    credit_count = models.PositiveIntegerField(null=True, blank=True)
    # Live remaining balance for a FLEX pass; decremented at redemption and
    # floored at 0 (status -> EXHAUSTED). Null for a season pass.
    credits_remaining = models.PositiveIntegerField(null=True, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    # Snapshot of product.events at purchase; EMPTY = all events.
    covered_events = models.ManyToManyField(
        "events.Event", blank=True, related_name="covering_passes"
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            # "This guest's passes" (portal) and "this org's passes by status"
            # (dashboard outstanding-liability report) are the two hot lookups.
            models.Index(fields=["organization", "guest"]),
            models.Index(fields=["organization", "status"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} pass for {self.buyer_label} ({self.status})"

    @property
    def buyer_label(self):
        if self.guest_id:
            return str(self.guest)
        return self.order.buyer_email if self.order_id else "unknown"


class PassRedemption(TenantScopedModel):
    """One admission handed out against a pass -- the join between a
    PassPurchase and a single minted Ticket, one row PER TICKET.

    Created by payments.services.fulfill_hold_with_pass when a holder redeems
    their pass for seats: the same inventory-consuming fulfillment the paid
    ticket path uses mints the Tickets (so a pass seat is as real, and as
    inventory-accurate, as a bought one), then this row records that the ticket
    was comped against the pass. `face_value` snapshots the comped ticket's
    price for reporting (outstanding-liability / redemption-value reports);
    `credits_used` is 1 for a flex redemption (it burned one credit) and 0 for
    a season redemption (season admissions aren't credit-metered).

    UNDOING A REDEMPTION = DELETING THIS ROW (restore_redemptions_for_order,
    on void/refund of the redemption order). For a season pass that frees the
    event slot; for a flex pass the credit is restored separately from the
    summed credits_used. See the module docstring.

    THE SEASON BACKSTOP (unique_season_event_redemption): a partial-unique
    constraint on (pass_purchase, event) WHERE credits_used=0 enforces "one
    season admission per covered event" at the DB, so a true race between two
    concurrent redemptions of the same season pass for the same event can't
    double-admit. Season rows always carry credits_used=0 so they're all
    covered; flex rows carry credits_used>=1 so they're EXEMPT (a flex holder
    may legitimately redeem several credits against one event). Same partial-
    constraint idiom as orders.Ticket.unique_live_ticket_per_performance_seat.
    """

    pass_purchase = models.ForeignKey(
        PassPurchase, on_delete=models.PROTECT, related_name="redemptions"
    )
    order = models.ForeignKey(
        "orders.Order", on_delete=models.CASCADE, related_name="pass_redemptions"
    )
    ticket = models.OneToOneField(
        "orders.Ticket", on_delete=models.CASCADE, related_name="pass_redemption"
    )
    performance = models.ForeignKey(
        "events.Performance", on_delete=models.PROTECT, related_name="pass_redemptions"
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.PROTECT, related_name="pass_redemptions"
    )
    seat = models.ForeignKey(
        "venues.Seat",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pass_redemptions",
    )
    # The comped ticket's face value, snapshotted for reporting -- what the
    # holder would have paid, i.e. the pass's realized value against this seat.
    face_value = models.DecimalField(max_digits=10, decimal_places=2)
    # 1 for a flex redemption (burned a credit), 0 for a season redemption
    # (one-per-event, not credit-metered). Also the partition key for the
    # season backstop constraint below (only credits_used=0 rows are guarded).
    credits_used = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "pass_purchase"]),
        ]
        constraints = [
            # Season backstop -- one admission per covered event. Only season
            # rows (credits_used=0) are constrained; flex rows (credits_used>=1)
            # are exempt so a flex holder can redeem multiple credits toward one
            # event. See class docstring.
            models.UniqueConstraint(
                fields=["pass_purchase", "event"],
                condition=models.Q(credits_used=0),
                name="unique_season_event_redemption",
            ),
        ]

    def __str__(self):
        return f"Redemption of pass #{self.pass_purchase_id} for {self.event} (ticket {self.ticket_id})"
