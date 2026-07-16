"""Vendor-agnostic fulfillment core: turns a paid hold/pass/donation into
Orders + Tickets + PassRedemptions. This module makes ZERO Stripe SDK calls
and must NEVER import `payments` -- the money path (payments/services.py's
Stripe gateway, and its `fulfill_checkout_session` webhook handler) imports
`fulfillment`, never the reverse. It is called by both the real Stripe
webhook and the env-gated test/stub checkout path (orders/views.py's
checkout_test / checkout_stub and their donation/pass counterparts), which is
the whole point of keeping it independent of any particular payment
provider: the same fulfillment logic runs whether the money arrived via a
real Stripe charge or a simulated one.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from events.models import GAAllocation
from guests import services as guest_services
from guests.models import GuestAccount
from orders import services as order_services
from orders.models import Order, OrderItem, Payment, Ticket
from passes import services as passes_services
from passes.models import PassProduct, PassPurchase, PassRedemption
from promotions import services as promotions_services
from venues.models import Seat


class FulfillmentError(Exception):
    """Base class for fulfill_checkout_session failures. These are caught by
    the webhook view (payments/views.py), logged, and acknowledged with a
    200 anyway -- see that view's docstring for why retrying wouldn't help."""


class HoldGoneError(FulfillmentError):
    """The Hold referenced by the session's metadata is missing or expired
    by the time payment completed (e.g. the buyer dawdled past the 10-minute
    hold window on Stripe's hosted page, or the hold sweeper already ran)."""


class AvailabilityChangedError(FulfillmentError):
    """Availability changed between hold creation and fulfillment. Shouldn't
    normally happen -- an active Hold already reserves its inventory against
    every *other* hold -- but re-checked defensively with the same
    lock-then-recheck pattern orders/services.py uses, per
    docs/ARCHITECTURE.md's "re-validate the hold" instruction."""


class DonationDataError(FulfillmentError):
    """A donation checkout session's metadata is missing or malformed (no
    parseable `donation_amount`) by the time payment completed. A
    FulfillmentError like HoldGoneError, so the webhook view acks 200 and logs
    rather than making Stripe retry a session that can never fulfill (the
    metadata won't heal on redelivery); a human reconciles the stray charge
    from the Stripe dashboard."""


class PassDataError(FulfillmentError):
    """A pass checkout session's metadata is missing/malformed, or names a pass
    product that's gone or inactive, by the time payment completed. A
    FulfillmentError like DonationDataError, so the webhook view acks 200 and
    logs rather than making Stripe retry a session that can never fulfill (the
    product won't come back on redelivery); a human reconciles the stray charge
    from the Stripe dashboard. Also raised by fulfill_pass_purchase when handed
    an inactive product directly."""


class PassRedemptionError(FulfillmentError):
    """Raised by fulfill_hold_with_pass when a pass can't be redeemed for the
    requested seats -- no credits left, the pass doesn't cover this
    show/date, a season pass was already used for this event, or the buyer
    tried to stack a promo/donation onto a redemption. The message is BUYER-
    SAFE (shown directly in the guest portal's "use my pass" flow), so it never
    carries internal detail. A FulfillmentError subclass so the webhook path
    treats it uniformly, but the redemption core is normally reached from the
    portal view, not Stripe (a $0 redemption doesn't go through Stripe)."""


# --- Shared fulfillment core ---------------------------------------------


@transaction.atomic
def fulfill_hold(hold, *, buyer_email, buyer_name, payment_ref, provider, stripe_checkout_session_id=None, marketing_opt_in=False):
    """Core order-fulfillment transaction, shared by EVERY payment path
    (the Stripe webhook below, and the env-gated TEST CHECKOUT path in
    orders/views.py). This is the one and only place that turns a Hold into
    a real Order + OrderItems + Tickets -- re-validate the hold, lock +
    recheck its inventory (GAAllocation row for GA, Seat rows for reserved
    -- the same targets orders/services.py locks, so this can't race a
    fresh set_ga_hold/set_reserved_hold call), create the Order
    (status=paid) + OrderItems + Tickets, bump GAAllocation.sold for GA,
    record a Payment row, and delete the Hold. `hold` must already have been
    looked up (and, for a session-based caller, tenant/session-scoped) by
    the caller -- this function only re-checks its expiry, not who's
    allowed to consume it.

    Raises HoldGoneError if `hold` has expired since the caller looked it
    up, or AvailabilityChangedError (via _fulfill_ga/_fulfill_reserved) if
    availability no longer holds. Nothing is written to the DB before the
    raise, thanks to @transaction.atomic (this function's own savepoint,
    nested inside whatever transaction the caller is in).

    ALWAYS creates a fresh Order -- it does not check for an existing one,
    so callers own idempotency:
      - fulfill_checkout_session (below) pre-checks "does an Order already
        exist for this session_id" before calling this, and wraps the call
        in an IntegrityError fallback for the concurrent-delivery race (see
        its docstring) -- that's why `stripe_checkout_session_id` is
        threaded through and written in the SAME Order.objects.create()
        call this function makes, rather than added after: two concurrent
        deliveries for the same session must collide on that create(), or
        the race protection is void.
      - The test-checkout path (orders/views.py's checkout_test) relies on
        the Hold itself: this function deletes it before returning, so a
        resubmitted test-checkout POST for the same hold hits
        Hold.DoesNotExist / a stale lookup in the view, not a second Order.

    provider/payment_ref are caller-supplied and recorded verbatim on the
    Payment row (e.g. provider="stripe", payment_ref=<payment_intent id>;
    or provider="test", payment_ref=f"test-{uuid4()}" for TEST CHECKOUT --
    see .env.example's ENABLE_TEST_CHECKOUT for why that path must stay
    off by default).

    Sending the ticket email is every caller's job, done AFTER this
    returns/commits -- email delivery shouldn't be able to roll back a paid
    order.
    """
    if hold.expires_at <= timezone.now():
        raise HoldGoneError(
            f"Hold {hold.pk} is missing or expired; payment succeeded but nothing was fulfilled."
        )

    organization = hold.organization
    # The NET (post-discount) total is what Stripe actually collected and what
    # a refund reverses, so it's what Order.total must record -- not the gross
    # subtotal. hold_grand_total = hold_total - hold_discount (floored at 0).
    # For a hold with no promo, hold_discount is 0.00 and this equals the gross,
    # so the un-discounted path is byte-for-byte unchanged.
    total = order_services.hold_grand_total(hold)

    # Attach (or create) the buyer's per-theater guest account so this order
    # shows up in their self-service portal (guests/). Keyed off buyer_email;
    # a blank email (possible on a Stripe session that carried none) just
    # leaves guest=None -- see GuestAccountManager.get_or_create_for_email.
    # Done inside this same transaction so a fulfilled order and its guest
    # link commit atomically.
    guest, _ = GuestAccount.objects.get_or_create_for_email(
        organization, buyer_email, name=buyer_name
    )
    # Phase 4 consent: if the buyer ticked the marketing opt-in box at checkout,
    # record it on their guest account. record_marketing_opt_in is idempotent
    # and ONE-WAY (it never opts anyone out), so a returning buyer who leaves
    # the box un-ticked -- marketing_opt_in defaults False here -- is never
    # silently unsubscribed; only an explicit opt-in adds consent. See
    # guests.services.record_marketing_opt_in.
    if marketing_opt_in:
        guest_services.record_marketing_opt_in(guest)

    order = Order.objects.create(
        organization=organization,
        performance=hold.performance,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        guest=guest,
        # Snapshot the promo off the hold onto the Order: promo_code_text is the
        # code as applied (the FK may later be archived/deleted), discount_amount
        # is the frozen deduction. Order.total above is already NET, so these two
        # are the order-level discount line that reconstructs the gross for
        # reporting (gross = total + discount_amount). See Order's docstring.
        promo_code_text=hold.promo_code_text or "",
        discount_amount=order_services.hold_discount(hold),
        total=total,
        status=Order.Status.PAID,
        stripe_checkout_session_id=stripe_checkout_session_id,
    )

    if hold.price_tier_id and hold.quantity:
        _fulfill_ga(organization, hold, order)
    else:
        _fulfill_reserved(organization, hold, order)

    # Phase 2 donation add-on: if the buyer added a gift to this cart, record
    # it as its own kind=DONATION OrderItem -- quantity 1, unit_amount the
    # snapshotted gift, no ticket minted and no inventory touched. Order.total
    # is already hold_grand_total (donation-inclusive, computed above), so
    # nothing changes there; this line is what makes the donation show up on
    # the order, feed the donations report, and carry its campaign provenance.
    donation = order_services.hold_donation(hold)
    if donation > Decimal("0.00"):
        OrderItem.objects.create(
            organization=organization,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=donation,
            donation_campaign=hold.donation_campaign,
        )

    Payment.objects.create(
        organization=organization,
        order=order,
        provider=provider,
        amount=total,
        status="succeeded",
        provider_ref=payment_ref,
    )

    # Count the redemption ONLY now, at fulfillment, inside this same
    # transaction -- so an abandoned cart never burns a use, and the increment
    # commits atomically with the Order it belongs to. record_redemption is an
    # F()-based DB update, so it can't lose a count to a concurrent fulfillment.
    #
    # DESIGN STANCE -- a paid order is NEVER rejected for promo state. The code
    # may have expired or hit its cap in the window between apply-to-cart and
    # this payment; we DO NOT re-validate here. The buyer already paid the
    # discounted amount Stripe collected, and refusing to hand out their tickets
    # over a soft-cap technicality would be the wrong trade-off -- the exact
    # OPPOSITE of HoldGoneError (where nothing was charged yet, so bailing is
    # correct). The cap is enforced softly at apply time (validate_code); a rare
    # over-cap-by-one under concurrent checkouts is accepted on purpose.
    if hold.promo_code_id:
        promotions_services.record_redemption(hold.promo_code)

    hold.delete()  # HoldSeat rows cascade-delete with it.
    return order


@transaction.atomic
def fulfill_donation(
    organization,
    *,
    amount,
    campaign,
    buyer_email,
    buyer_name,
    provider,
    payment_ref,
    stripe_checkout_session_id=None,
    marketing_opt_in=False,
):
    """Turn a standalone (hold-less) donation into a paid Order + one
    kind=DONATION OrderItem + a Payment. The sibling of fulfill_hold for the
    /donate/ path (a gift with no tickets, so no cart/hold ever existed).

    WHY A SIBLING, NOT A BRANCH OF fulfill_hold: fulfill_hold is organized
    entirely around a Hold -- it re-checks the hold's expiry, locks and
    re-validates the hold's inventory (GAAllocation / Seat rows), mints
    tickets from the held seats, and deletes the hold at the end. A standalone
    donation has NONE of that: no hold, no performance, no inventory, no
    ticket. Threading a "maybe there's no hold" flag through every one of those
    steps would riddle the ticket money path with donation-only special cases
    for no shared benefit. Keeping this a small independent transaction that
    creates exactly the three rows a donation needs is clearer and keeps the
    ticket path honest. The pieces they DO share (guest get_or_create, the
    donation OrderItem shape, the Payment row) are small and identical by
    construction.

    Order.performance is left NULL -- a donation reserves no performance (see
    Order.performance's v1 rule). Order.total is the gift amount; the promo
    fields are untouched (a standalone gift has no code). Idempotency is the
    caller's job, exactly as for fulfill_hold: the Stripe caller
    (fulfill_checkout_session) pre-checks the session id and wraps this in the
    same IntegrityError->winner fallback, which is why
    stripe_checkout_session_id is written in this same Order.objects.create().

    Sending the acknowledgment/receipt email is the caller's job, done AFTER
    this commits."""
    guest, _ = GuestAccount.objects.get_or_create_for_email(
        organization, buyer_email, name=buyer_name
    )
    # Phase 4 consent (idempotent, one-way -- see fulfill_hold / guests.services).
    if marketing_opt_in:
        guest_services.record_marketing_opt_in(guest)

    order = Order.objects.create(
        organization=organization,
        performance=None,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        guest=guest,
        total=amount,
        status=Order.Status.PAID,
        stripe_checkout_session_id=stripe_checkout_session_id,
    )
    OrderItem.objects.create(
        organization=organization,
        order=order,
        kind=OrderItem.Kind.DONATION,
        quantity=1,
        unit_amount=amount,
        donation_campaign=campaign,
    )
    Payment.objects.create(
        organization=organization,
        order=order,
        provider=provider,
        amount=amount,
        status="succeeded",
        provider_ref=payment_ref,
    )
    return order


@transaction.atomic
def fulfill_pass_purchase(
    organization,
    *,
    product,
    buyer_email,
    buyer_name,
    provider,
    payment_ref,
    stripe_checkout_session_id=None,
    marketing_opt_in=False,
):
    """Turn a paid one-time PASS purchase into a paid Order + one kind=PASS
    OrderItem + the PassPurchase entitlement row + a Payment. The sibling of
    fulfill_donation for the pass-purchase path (a pass bought outright, no
    tickets and no hold). NOT to be confused with fulfill_hold_with_pass, which
    is the REDEMPTION path (spending a pass on seats) -- this is the SALE.

    THE SNAPSHOT MOMENT: every entitlement term is frozen off `product` onto the
    new PassPurchase here and now (kind, credit_count, credits_remaining
    starting at the flex credit_count, the valid window, and covered_events as a
    copy of product.events) -- see passes.models' snapshot rule. A later edit to
    the product never touches this sold pass.

    Order.performance is left NULL (a pass reserves no performance, same v1 rule
    as a donation). Order.total is the pass price; promo fields untouched.
    Rejects an INACTIVE product with PassDataError -- an archived/disabled pass
    must not be sold even if a stale checkout got this far (the storefront
    shouldn't have offered it). Idempotency is the caller's job exactly as for
    fulfill_donation: the Stripe caller pre-checks the session id and wraps this
    in the same IntegrityError->winner fallback, which is why
    stripe_checkout_session_id is written in this same Order.objects.create().

    Sending the pass receipt email is the caller's job, done AFTER this commits.
    """
    if not product.is_active:
        raise PassDataError(
            f"Pass product {product.pk} is inactive; payment succeeded but the pass was "
            "not issued."
        )

    guest, _ = GuestAccount.objects.get_or_create_for_email(
        organization, buyer_email, name=buyer_name
    )
    # Phase 4 consent (idempotent, one-way -- see fulfill_hold / guests.services).
    if marketing_opt_in:
        guest_services.record_marketing_opt_in(guest)

    order = Order.objects.create(
        organization=organization,
        performance=None,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        guest=guest,
        total=product.price,
        status=Order.Status.PAID,
        stripe_checkout_session_id=stripe_checkout_session_id,
    )
    OrderItem.objects.create(
        organization=organization,
        order=order,
        kind=OrderItem.Kind.PASS,
        quantity=1,
        unit_amount=product.price,
        pass_product=product,
    )
    purchase = PassPurchase.objects.create(
        organization=organization,
        product=product,
        guest=guest,
        order=order,
        # All entitlement terms snapshotted off the product at THIS moment.
        kind=product.kind,
        credit_count=product.credit_count,
        # Flex starts with a full balance; season carries no credit balance
        # (product.credit_count is null for a season product).
        credits_remaining=product.credit_count,
        valid_from=product.valid_from,
        valid_until=product.valid_until,
        status=PassPurchase.Status.ACTIVE,
    )
    # Snapshot the covered-event set (EMPTY = all events, preserved as empty).
    purchase.covered_events.set(product.events.all())

    Payment.objects.create(
        organization=organization,
        order=order,
        provider=provider,
        amount=product.price,
        status="succeeded",
        provider_ref=payment_ref,
    )
    return order


@transaction.atomic
def fulfill_hold_with_pass(hold, pass_purchase, *, buyer_email, buyer_name, marketing_opt_in=False):
    """THE REDEMPTION CORE: spend `pass_purchase` on the seats held by `hold`,
    minting real Tickets against real inventory for a $0 charge and recording a
    PassRedemption per ticket. The pass analogue of fulfill_hold -- it reuses
    the SAME inventory lock+recheck and Ticket-minting helpers
    (_fulfill_ga/_fulfill_reserved), so a pass seat consumes inventory exactly
    as a bought one does and can't double-book -- but the money is entitlement,
    not a charge.

    Reached from the guest portal's "use my pass" flow (a $0 order never goes
    through Stripe). Raises HoldGoneError if the hold expired, or
    PassRedemptionError (buyer-safe) for every "can't redeem this" reason.

    Steps:
      1. Hold expiry re-check (HoldGoneError, like fulfill_hold).
      2. Strict v1: reject a hold carrying a promo or a donation -- a redemption
         is $0 and can't also discount or collect a gift; the buyer removes them
         first (PassRedemptionError).
      3. Lock the PassPurchase row (select_for_update) and re-check under the
         lock: redeemable_now, pass_covers_performance(hold.performance), and the
         per-kind admission budget --
           FLEX: ticket_count <= credits_remaining.
           SEASON: ticket_count == 1 AND no existing redemption for this
                   (pass, event) -- one admission per covered event.
      4. Create the $0 Order (performance=hold.performance, PAID, total=0.00),
         then mint tickets + ticket OrderItems via the shared inventory-locking
         helpers. Order.total stays 0 even though the ticket OrderItems carry
         their snapshot face values -- Σitems != total is an established
         precedent here (a promo order's items are gross while total is net; see
         Order.total's docstring).
      5. One PassRedemption per minted Ticket (credits_used=1 flex / 0 season,
         face_value from the ticket's OrderItem snapshot).
      6. FLEX: decrement credits_remaining by ticket_count; status=EXHAUSTED at 0.
      7. A $0 Payment(provider="pass") for the audit trail.
      8. Delete the hold.

    THE RACE PATH: the unique_season_event_redemption constraint can still fire
    under a true concurrent double-redeem of the same season pass for the same
    event (both passing the step-3 recheck before either commits). Caught here
    and re-raised as PassRedemptionError -- the same "let the DB constraint be
    the backstop, translate its IntegrityError into the buyer-safe failure it
    represents" pattern fulfill_checkout_session uses for AvailabilityChangedError.
    """
    if hold.expires_at <= timezone.now():
        raise HoldGoneError(
            f"Hold {hold.pk} is missing or expired; the pass was not redeemed."
        )

    # v1 STRICT: a redemption is a $0 order -- a promo discount or a cart
    # donation has nowhere to land on it. Make the buyer clear them rather than
    # silently dropping money they thought they were giving/saving.
    if hold.promo_code_id or hold.promo_code_text:
        raise PassRedemptionError(
            "Remove the promo code before redeeming a pass -- a pass redemption "
            "is already free."
        )
    if order_services.hold_donation(hold) > Decimal("0.00"):
        raise PassRedemptionError(
            "Remove the donation before redeeming a pass, then donate separately."
        )

    organization = hold.organization

    # Lock and re-fetch the pass under the lock so a concurrent redemption of
    # the SAME pass can't both pass the budget check (Postgres row lock;
    # SQLite's IMMEDIATE-mode whole-DB lock does the same -- see
    # orders.services' locking note).
    pass_purchase = (
        PassPurchase.objects.select_for_update().get(pk=pass_purchase.pk)
    )

    if pass_purchase.organization_id != organization.pk:
        # Defense in depth -- the caller scopes the pass to the tenant, but a
        # cross-tenant pass must never redeem against this org's inventory.
        raise PassRedemptionError("This pass can't be used here.")

    if not passes_services.redeemable_now(pass_purchase):
        raise PassRedemptionError("This pass is no longer active.")

    if not passes_services.pass_covers_performance(pass_purchase, hold.performance):
        raise PassRedemptionError("This pass doesn't cover this performance.")

    # How many admissions this hold wants.
    if hold.price_tier_id and hold.quantity:
        ticket_count = hold.quantity
    else:
        ticket_count = hold.hold_seats.count()
    if ticket_count <= 0:
        raise PassRedemptionError("There's nothing to redeem.")

    is_flex = pass_purchase.kind == PassProduct.Kind.FLEX
    if is_flex:
        if ticket_count > (pass_purchase.credits_remaining or 0):
            raise PassRedemptionError("This pass doesn't have enough credits left.")
    else:
        # Season: one admission per covered event.
        if ticket_count != 1:
            raise PassRedemptionError(
                "A season pass admits one seat per show -- redeem a single seat."
            )
        already = pass_purchase.redemptions.filter(
            event=hold.performance.event
        ).exists()
        if already:
            raise PassRedemptionError("This pass was already used for this show.")

    # Attach/lookup the buyer's guest -- prefer the pass's own guest (the pass
    # holder), falling back to the buyer_email get_or_create like the ticket
    # path so a redemption always has a guest to hang on the portal.
    guest = pass_purchase.guest
    if guest is None:
        guest, _ = GuestAccount.objects.get_or_create_for_email(
            organization, buyer_email, name=buyer_name
        )
    # Phase 4 consent: opt in WHICHEVER guest this redemption links (the pass
    # holder if the pass carries one, else the buyer's get-or-created guest).
    # Idempotent + one-way, like every other fulfill_* path.
    if marketing_opt_in:
        guest_services.record_marketing_opt_in(guest)

    order = Order.objects.create(
        organization=organization,
        performance=hold.performance,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        guest=guest,
        # A redemption charges nothing -- the entitlement already paid for it.
        # Ticket OrderItems below still carry their snapshot face values, so
        # Σitems != total here (0.00); that's the established promo precedent.
        total=Decimal("0.00"),
        status=Order.Status.PAID,
    )

    # Mint tickets + ticket OrderItems + consume inventory via the SAME helpers
    # the paid path uses -- they lock the GAAllocation / Seat rows and re-check
    # availability, so a pass redemption can't double-book against a concurrent
    # sale. (They raise AvailabilityChangedError / IntegrityError on a seat
    # race, handled by the caller exactly as for a paid hold.)
    if hold.price_tier_id and hold.quantity:
        _fulfill_ga(organization, hold, order)
    else:
        _fulfill_reserved(organization, hold, order)

    # One PassRedemption per minted ticket. face_value comes from the ticket's
    # OrderItem snapshot (GA: the single GA line's unit_amount; reserved: the
    # per-seat line's). credits_used is 1 for flex (burned a credit) / 0 for
    # season (one-per-event, exempt from the season backstop constraint).
    ga_item = None
    seat_amounts = {}
    for item in order.items.all():
        if item.seat_id is None:
            ga_item = item
        else:
            seat_amounts[item.seat_id] = item.unit_amount

    credits_used = 1 if is_flex else 0
    try:
        for ticket in order.tickets.all():
            if ticket.seat_id is None:
                face_value = ga_item.unit_amount if ga_item is not None else Decimal("0.00")
            else:
                face_value = seat_amounts.get(ticket.seat_id, Decimal("0.00"))
            PassRedemption.objects.create(
                organization=organization,
                pass_purchase=pass_purchase,
                order=order,
                ticket=ticket,
                performance=hold.performance,
                event=hold.performance.event,
                seat=ticket.seat,
                face_value=face_value,
                credits_used=credits_used,
            )
    except IntegrityError as exc:
        # The season backstop fired under a true concurrent double-redeem of
        # this event -- translate the raw constraint violation into the
        # buyer-safe failure it represents (same stance as
        # fulfill_checkout_session's AvailabilityChangedError translation).
        raise PassRedemptionError(
            "This pass was already used for this show."
        ) from exc

    # Flex: burn the credits now, inside this same transaction. Floored at 0;
    # a pass that hits 0 is EXHAUSTED (redeemable_now will reject it thereafter,
    # until credits are restored by a void).
    if is_flex:
        pass_purchase.credits_remaining = max(
            (pass_purchase.credits_remaining or 0) - ticket_count, 0
        )
        update_fields = ["credits_remaining"]
        if pass_purchase.credits_remaining == 0:
            pass_purchase.status = PassPurchase.Status.EXHAUSTED
            update_fields.append("status")
        pass_purchase.save(update_fields=update_fields)

    Payment.objects.create(
        organization=organization,
        order=order,
        provider="pass",
        amount=Decimal("0.00"),
        status="succeeded",
        provider_ref=f"pass-{pass_purchase.pk}",
    )

    hold.delete()  # HoldSeat rows cascade-delete with it.
    return order


def _fulfill_ga(organization, hold, order):
    allocation = GAAllocation.objects.select_for_update().get(performance=hold.performance)
    if allocation.sold + hold.quantity > allocation.capacity:
        raise AvailabilityChangedError(
            f"GA allocation for performance {hold.performance_id} no longer has room for "
            f"hold {hold.pk} ({hold.quantity} seat(s))."
        )

    OrderItem.objects.create(
        organization=organization,
        order=order,
        price_tier=hold.price_tier,
        seat=None,
        quantity=hold.quantity,
        # Record the snapshotted GA price (see order_services.ga_unit_amount),
        # so the OrderItem matches both hold_total and the Stripe line item
        # even if the tier was edited between hold creation and fulfillment.
        unit_amount=order_services.ga_unit_amount(hold),
    )
    Ticket.objects.bulk_create(
        [
            Ticket(
                organization=organization,
                order=order,
                performance=hold.performance,
                seat=None,
                holder_name=order.buyer_name,
            )
            for _ in range(hold.quantity)
        ]
    )

    allocation.sold += hold.quantity
    allocation.save(update_fields=["sold"])


def _fulfill_reserved(organization, hold, order):
    hold_seats = list(
        hold.hold_seats.select_related("seat", "price_tier", "pricing_zone").order_by("seat_id")
    )
    seat_ids = [hold_seat.seat_id for hold_seat in hold_seats]

    # Lock the same Seat rows orders.services.set_reserved_hold locks,
    # ordered by pk, before re-checking -- see that module's docstring for
    # why this makes the recheck race-safe on both SQLite and Postgres.
    list(Seat.objects.select_for_update().filter(pk__in=seat_ids).order_by("pk"))

    ticketed_seat_ids = set(
        Ticket.objects.filter(performance=hold.performance, seat_id__in=seat_ids)
        .exclude(status=Ticket.Status.VOID)
        .values_list("seat_id", flat=True)
    )
    if ticketed_seat_ids:
        raise AvailabilityChangedError(
            f"Seat(s) {sorted(ticketed_seat_ids)} for performance {hold.performance_id} are "
            f"already ticketed; hold {hold.pk} can't be fulfilled."
        )

    for hold_seat in hold_seats:
        # Phase C: OrderItem.unit_amount is copied straight from the
        # HoldSeat's own snapshot (never re-resolved here), so a zone/tier
        # price edit between hold creation and fulfillment can never change
        # what this order actually gets charged -- see HoldSeat's and
        # OrderItem's docstrings.
        OrderItem.objects.create(
            organization=organization,
            order=order,
            price_tier=hold_seat.price_tier,
            pricing_zone=hold_seat.pricing_zone,
            seat=hold_seat.seat,
            quantity=1,
            unit_amount=hold_seat.unit_amount,
        )
        Ticket.objects.create(
            organization=organization,
            order=order,
            performance=hold.performance,
            seat=hold_seat.seat,
            holder_name=order.buyer_name,
        )
