"""Pass entitlement service layer: the pure, money-path-agnostic predicates
and helpers that decide whether a pass covers a performance, whether it can be
redeemed right now, how many admissions it has left, and -- crucially -- how to
UNDO a redemption's entitlement consumption when the redemption order is voided.

DEPENDENCY DIRECTION (identical stance to donations/promotions): this module
must NOT import from orders or payments. The money path (payments.services
fulfill_pass_purchase / fulfill_hold_with_pass, and refund_order) depends on
passes, never the reverse. Everything here operates on PassPurchase /
PassRedemption rows and on plain performance objects the CALLER passes in, so
nothing reaches back into the ticket money path. This keeps the redemption
core (which lives in payments, where the inventory lock and Ticket minting are)
free to lean on these predicates without an import cycle.
"""

from collections import defaultdict

from django.utils import timezone

from .models import PassProduct, PassPurchase


def get_active_products(organization):
    """`organization`'s sellable passes -- the storefront listing. Active only
    (is_active doubles as the enable/archive switch, see PassProduct), newest
    first is left to the caller's ordering needs; returns a QuerySet so callers
    can filter/paginate further."""
    return PassProduct.objects.filter(organization=organization, is_active=True)


def pass_covers_performance(pass_purchase, performance):
    """Does `pass_purchase` cover `performance`? Two independent gates, BOTH
    read off the purchase's frozen snapshots (never the live product):

    1. EVENT coverage: covered_events EMPTY = all events (the load-bearing
       empty case -- an all-access pass), otherwise the performance's event
       must be in the snapshotted set.
    2. TIME coverage: performance.starts_at must fall within
       [valid_from, valid_until]; a null bound is open on that side.

    Pure predicate -- no DB writes, no money-path imports. `performance` is
    whatever the caller (the redemption path) already has in hand."""
    # Event gate. An empty covered_events set = all events; a non-empty set
    # restricts to exactly its members.
    if pass_purchase.covered_events.exists():
        if not pass_purchase.covered_events.filter(pk=performance.event_id).exists():
            return False

    # Time gate. Null bounds are open.
    starts_at = performance.starts_at
    if pass_purchase.valid_from is not None and starts_at < pass_purchase.valid_from:
        return False
    if pass_purchase.valid_until is not None and starts_at > pass_purchase.valid_until:
        return False
    return True


def redeemable_now(pass_purchase, now=None):
    """Can `pass_purchase` be redeemed at `now` (default: current time) at all,
    independent of any particular performance? True when the pass is ACTIVE and
    not past its valid_until (null = open-ended). This is the coarse "is this
    pass live" gate; per-performance coverage (event membership + valid_from) is
    pass_covers_performance's job.

    Deliberately does NOT re-check credits_remaining -- a flex pass at 0 credits
    is already status=EXHAUSTED (set at the decrement), so the status gate
    covers it; keeping this predicate about pass liveness (not balance) leaves
    the credit check to the redemption path, which needs the exact ticket count
    to compare against anyway."""
    if now is None:
        now = timezone.now()
    if pass_purchase.status != PassPurchase.Status.ACTIVE:
        return False
    if pass_purchase.valid_until is not None and now > pass_purchase.valid_until:
        return False
    return True


def remaining_admissions(pass_purchase):
    """How many more admissions `pass_purchase` can hand out, or None when that
    is unbounded.

    - FLEX: credits_remaining (the live balance).
    - SEASON with a bounded covered_events set: covered-event count minus the
      number of events already redeemed against (one admission per covered
      event, so the remaining headroom is exactly the un-redeemed events).
    - SEASON with an EMPTY covered_events set: an all-events season pass is
      UNBOUNDED (one admission to every event the theater has ever run or will
      run) -- there's no finite covered-event count to subtract from, so this
      returns None and documents it. Callers treat None as "no numeric cap to
      display" rather than zero.
    """
    if pass_purchase.kind == PassProduct.Kind.FLEX:
        return pass_purchase.credits_remaining

    # Season.
    covered_count = pass_purchase.covered_events.count()
    if covered_count == 0:
        # All-events season pass -- unbounded, no finite headroom to report.
        return None
    # Season redemptions are one-per-event and always credits_used=0; count
    # distinct events already admitted and subtract from the covered set.
    redeemed_events = (
        pass_purchase.redemptions.values_list("event_id", flat=True).distinct().count()
    )
    return max(covered_count - redeemed_events, 0)


def restore_redemptions_for_order(order):
    """Undo the entitlement consumption of `order`'s pass redemptions and
    return how many redemption rows were reversed. Called from the money path
    (payments.services.refund_order, after void_order) when a REDEMPTION order
    -- one that used a pass to comp its tickets -- is voided/refunded, so the
    holder gets their season slots and flex credits BACK.

    Mechanics:
      - Delete every PassRedemption on `order` (that's what frees a season
        event slot -- the partial-unique constraint stops guarding that event
        the moment the row is gone).
      - For flex passes, sum the deleted rows' credits_used back onto each
        PassPurchase.credits_remaining, and flip status EXHAUSTED -> ACTIVE when
        credits return (a pass that hit 0 becomes usable again). Season rows
        carry credits_used=0, so this adds nothing for them -- correct, a season
        slot is freed by the delete alone.

    ATOMIC-SAFE UNDER THE CALLER'S TRANSACTION: this does not open its own
    transaction (refund_order is already @transaction.atomic and wraps
    void_order + this call), and it select_for_update()s the PassPurchase rows
    before mutating their balances so a concurrent redemption of the same pass
    can't lose or duplicate a credit restore. A no-op returning 0 when the order
    has no redemptions (the common case -- most orders aren't pass redemptions).
    """
    redemptions = list(order.pass_redemptions.all())
    if not redemptions:
        return 0

    credits_by_pass = defaultdict(int)
    for redemption in redemptions:
        credits_by_pass[redemption.pass_purchase_id] += redemption.credits_used

    # Lock the affected passes before touching their balances (Postgres row
    # lock; SQLite's IMMEDIATE-mode whole-DB lock does the same -- see
    # orders.services' module locking note).
    locked_passes = {
        purchase.pk: purchase
        for purchase in PassPurchase.objects.select_for_update().filter(
            pk__in=credits_by_pass.keys()
        )
    }

    count = len(redemptions)
    order.pass_redemptions.all().delete()

    for pass_id, restored in credits_by_pass.items():
        purchase = locked_passes.get(pass_id)
        if purchase is None:
            continue
        update_fields = []
        if purchase.credits_remaining is not None and restored:
            purchase.credits_remaining = purchase.credits_remaining + restored
            update_fields.append("credits_remaining")
        # A flex pass that had run dry is usable again now that credits are
        # back. (Season passes never go EXHAUSTED, so this only ever fires for
        # flex.) A REFUNDED pass stays refunded -- restoring a redemption on a
        # refunded purchase shouldn't resurrect it.
        if (
            purchase.status == PassPurchase.Status.EXHAUSTED
            and (purchase.credits_remaining or 0) > 0
        ):
            purchase.status = PassPurchase.Status.ACTIVE
            update_fields.append("status")
        if update_fields:
            purchase.save(update_fields=update_fields)

    return count
