"""Stripe integration: Checkout Session creation and the
checkout.session.completed webhook's fulfillment logic (order/ticket
creation). See docs/ARCHITECTURE.md "Checkout" for the end-to-end flow this
implements.

Per-tenant Stripe accounts (white label): every Stripe SDK call below takes
an explicit `api_key=` / verifies against an explicit webhook secret pulled
from the Organization row involved in *that* call, instead of setting
`stripe.api_key` globally. stripe-python supports this natively -- `api_key`
is one of the "request options" `CreateableAPIResource.create()` strips out
of `**params` before making the HTTP call (see
`stripe._api_resource.APIResource._static_request` /
`extract_options_from_dict` in the installed SDK). That per-request key is
what makes it safe for two tenants' checkouts to be created concurrently in
the same gunicorn worker without ever racing on a shared global -- there is
no shared global. Confirmed against the Stripe Python SDK docs via the
Stripe MCP (stripe-python is currently in its v15.x line; requirements.txt
pins a wide `stripe>=11,<16` range).

Idempotency: `fulfill_checkout_session` is keyed on
`Order.stripe_checkout_session_id`. Stripe retries webhook delivery (e.g. on
a slow response or a network blip), and Checkout Sessions are otherwise
one-shot, so "does an Order already exist for this session id" is both
necessary and sufficient to detect a replay -- see its docstring.
"""

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

import stripe

from events.models import GAAllocation
from orders import services as order_services
from orders.models import Hold, Order, OrderItem, Payment, Ticket
from venues.models import Seat


class CheckoutError(Exception):
    """Raised by create_checkout_session for a hold that can't be checked
    out right now (expired, empty). Message is safe to flash to the buyer."""


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


class TenantMismatchError(FulfillmentError):
    """The session's metadata.organization_id doesn't match the organization
    whose webhook secret verified it. Shouldn't be reachable in practice
    (each org's Stripe webhook endpoint is configured with that org's own
    signing secret, so a payload that verifies against org A's secret was,
    by construction, sent to org A's endpoint about an org A session) --
    kept as defense-in-depth against a misconfigured webhook endpoint."""


# --- Checkout Session creation ------------------------------------------


def create_checkout_session(hold, request):
    """Create a Stripe Checkout Session for `hold` and return its hosted
    payment page URL. Does NOT create an Order/Payment/Ticket -- those are
    created by fulfill_checkout_session() once Stripe confirms payment via
    the checkout.session.completed webhook. `request` supplies the tenant
    host (for success/cancel URLs) and nothing else; it is never sent to
    Stripe.
    """
    if hold.expires_at <= timezone.now():
        raise CheckoutError("Your hold has expired. Please make your selection again.")

    line_items = _line_items_for_hold(hold)
    if not line_items:
        raise CheckoutError("There's nothing to check out.")

    organization = hold.organization
    success_url = request.build_absolute_uri(reverse("checkout_success")) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = request.build_absolute_uri(reverse("checkout_cancel"))

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"hold_id": str(hold.pk), "organization_id": str(organization.pk)},
        api_key=organization.stripe_secret_key,
    )
    return session.url


def _line_items_for_hold(hold):
    """GA: a single line item, quantity x price_tier.amount. Reserved: one
    line item PER HoldSeat at its own price_tier.amount (seats can span
    sections/tiers within one hold, so they can't be collapsed into one
    line). Ad-hoc `price_data` is used instead of pre-created Stripe Price
    objects since tiers are defined and priced entirely on our side."""
    if hold.price_tier_id and hold.quantity:
        tier = hold.price_tier
        return [
            {
                "quantity": hold.quantity,
                "price_data": {
                    "currency": tier.currency.lower(),
                    "unit_amount": _to_minor_units(tier.amount),
                    "product_data": {
                        "name": f"{hold.performance.event.title} — {tier.name}",
                    },
                },
            }
        ]

    line_items = []
    for hold_seat in hold.hold_seats.select_related("seat__section", "price_tier"):
        seat = hold_seat.seat
        tier = hold_seat.price_tier
        line_items.append(
            {
                "quantity": 1,
                "price_data": {
                    "currency": tier.currency.lower(),
                    "unit_amount": _to_minor_units(tier.amount),
                    "product_data": {
                        "name": (
                            f"{hold.performance.event.title} — {seat.section.name} "
                            f"{seat.row_label}{seat.number}"
                        ),
                    },
                },
            }
        )
    return line_items


def _to_minor_units(amount):
    """Decimal dollars -> integer minor units (e.g. cents). Exact: every
    PriceTier.amount has exactly 2 decimal places, so amount * 100 is always
    an integral Decimal -- no float rounding involved."""
    return int((amount * 100).to_integral_value())


# --- Webhook fulfillment (checkout.session.completed) --------------------


@transaction.atomic
def fulfill_checkout_session(organization, session):
    """Turn a paid Stripe Checkout Session into an Order + OrderItems +
    Tickets. Called by the webhook view after signature verification.

    Idempotent: if an Order already exists for `session["id"]` (a Stripe
    retry, or this handler running twice), returns that Order unchanged with
    created=False and does nothing else -- no duplicate Order/Tickets, no
    double-incrementing GAAllocation.sold. Otherwise, inside this single
    transaction: re-validate + lock the Hold's inventory (GAAllocation row
    for GA, Seat rows for reserved -- the same targets orders/services.py
    locks, so a fulfillment can't race a fresh set_ga_hold/set_reserved_hold
    call), create the Order (status=paid) + OrderItems + Tickets, bump
    GAAllocation.sold for GA, delete the Hold, and record a Payment row.

    Returns (order, created). Raises FulfillmentError (never touches the
    DB before the raise, thanks to @transaction.atomic) if the Hold is gone/
    expired or availability no longer holds. Sending the ticket email is the
    caller's job, done AFTER this returns/commits -- email delivery
    shouldn't be able to roll back a paid order.
    """
    session_id = session["id"]
    existing = Order.objects.filter(organization=organization, stripe_checkout_session_id=session_id).first()
    if existing is not None:
        return existing, False

    metadata = session.get("metadata") or {}
    hold_id = metadata.get("hold_id")
    session_org_id = metadata.get("organization_id")
    if session_org_id and str(session_org_id) != str(organization.pk):
        raise TenantMismatchError(
            f"Session {session_id} metadata organization_id={session_org_id} does not match "
            f"the organization ({organization.pk}) whose webhook secret verified it."
        )

    hold = (
        Hold.objects.select_related("performance", "price_tier")
        .filter(organization=organization, pk=hold_id)
        .first()
    )
    if hold is None or hold.expires_at <= timezone.now():
        raise HoldGoneError(
            f"Hold {hold_id!r} for session {session_id} is missing or expired; payment succeeded "
            "but nothing was fulfilled."
        )

    customer_details = session.get("customer_details") or {}
    buyer_email = customer_details.get("email") or ""
    buyer_name = customer_details.get("name") or ""
    total = order_services.hold_total(hold)

    order = Order.objects.create(
        organization=organization,
        performance=hold.performance,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        total=total,
        status=Order.Status.PAID,
        stripe_checkout_session_id=session_id,
    )

    if hold.price_tier_id and hold.quantity:
        _fulfill_ga(organization, hold, order)
    else:
        _fulfill_reserved(organization, hold, order)

    Payment.objects.create(
        organization=organization,
        order=order,
        provider="stripe",
        amount=total,
        status="succeeded",
        provider_ref=session.get("payment_intent") or session_id,
    )

    hold.delete()  # HoldSeat rows cascade-delete with it.
    return order, True


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
        unit_amount=hold.price_tier.amount,
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
    hold_seats = list(hold.hold_seats.select_related("seat", "price_tier").order_by("seat_id"))
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
        OrderItem.objects.create(
            organization=organization,
            order=order,
            price_tier=hold_seat.price_tier,
            seat=hold_seat.seat,
            quantity=1,
            unit_amount=hold_seat.price_tier.amount,
        )
        Ticket.objects.create(
            organization=organization,
            order=order,
            performance=hold.performance,
            seat=hold_seat.seat,
            holder_name=order.buyer_name,
        )
