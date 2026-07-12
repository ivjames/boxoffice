"""Stripe integration: Connect (Express) onboarding, Checkout Session
creation, and the checkout.session.completed webhook's fulfillment logic
(order/ticket creation). See docs/ARCHITECTURE.md "Checkout" for the
end-to-end flow this implements.

Stripe Connect (Express), direct charges: boxo.show is the platform Stripe
account (settings.STRIPE_SECRET_KEY); every theater is a CONNECTED account
(Organization.stripe_account_id, an `acct_…`). There are no per-tenant secret
keys. Every SDK call uses the ONE platform key and selects the theater with
the request option `stripe_account=<acct_id>` (the `Stripe-Account` header).
Checkout Sessions are created *on the connected account* (a direct charge),
so the theater is merchant of record — its name is on the buyer's statement,
it bears its own Stripe fees and disputes — and the platform's cut rides
along as `payment_intent_data.application_fee_amount` (see
application_fee_amount() below). `stripe_account`, like `api_key`, is a
stripe-python "request option" stripped out of `**params` before the HTTP
call, so passing it per-call (rather than a shared global) is what keeps two
theaters' checkouts safe to create concurrently in one gunicorn worker.

Webhooks are a SINGLE platform Connect endpoint verified against
settings.STRIPE_WEBHOOK_SECRET; the connected account each event belongs to
arrives in the event's top-level `account` field (see payments/views.py),
not the Host header. There is no per-tenant webhook secret anymore.

Idempotency: `fulfill_checkout_session` is keyed on
`Order.stripe_checkout_session_id`. Stripe retries webhook delivery (e.g. on
a slow response or a network blip), and Checkout Sessions are otherwise
one-shot, so "does an Order already exist for this session id" is both
necessary and sufficient to detect a replay -- see its docstring.

`fulfill_hold()` is the shared core both `fulfill_checkout_session` (Stripe)
and the env-gated TEST CHECKOUT path (orders/views.py's checkout_test, only
reachable when settings.ENABLE_TEST_CHECKOUT is True -- see
config/settings/base.py and .env.example) call to actually turn a Hold into
an Order + Tickets. There is exactly one code path that hands out tickets;
the two callers differ only in idempotency strategy and where buyer_email/
buyer_name/payment_ref come from.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

import stripe

from events.models import GAAllocation
from guests.models import GuestAccount
from orders import services as order_services
from orders.models import Hold, Order, OrderItem, Payment, Ticket
from venues.models import Seat

logger = logging.getLogger(__name__)


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


class RefundError(Exception):
    """Raised by refund_order when Stripe won't process the refund. The
    message may carry Stripe API detail, so views flash a generic version and
    log this one."""


class TenantMismatchError(FulfillmentError):
    """The session's metadata.organization_id doesn't match the organization
    resolved from the event's connected account (`event.account` ->
    Organization.stripe_account_id). Shouldn't be reachable in practice — the
    same code set both when the session was created — but re-checked as
    defense-in-depth against a metadata/account mismatch before we hand out
    tickets against the wrong tenant."""


# --- Connect (Express) onboarding ----------------------------------------


class ConnectError(Exception):
    """Raised by the onboarding helpers when Stripe can't create/refresh a
    connected account. Message is logged; a generic version is flashed to the
    staff member (it may carry Stripe API detail not meant for the buyer)."""


def create_onboarding_link(organization, *, return_url, refresh_url):
    """Ensure `organization` has a Stripe Connect (Express) account and return
    a one-time Account Link URL to send the theater to Stripe's hosted
    onboarding. Creates the connected account on first call (storing its
    `acct_…` on the org) and reuses it thereafter, so a theater that abandons
    onboarding and comes back resumes the same account rather than spawning a
    new one. The platform key (settings.STRIPE_SECRET_KEY) authenticates all
    of this; the theater never sees or holds a key.

    Account Links are single-use and short-lived — this is why onboarding is a
    redirect generated fresh each time, not a stored URL. `refresh_url` is
    where Stripe bounces the user if the link is reused/expired (loop them
    back through here); `return_url` is where Stripe sends them when they
    finish (the return view then calls refresh_account_status)."""
    try:
        if not organization.stripe_account_id:
            account = stripe.Account.create(
                type="express",
                email=organization.contact_email or None,
                business_profile={"name": organization.name},
                metadata={"organization_id": str(organization.pk)},
                api_key=settings.STRIPE_SECRET_KEY,
            )
            organization.stripe_account_id = account.id
            organization.save(update_fields=["stripe_account_id"])

        link = stripe.AccountLink.create(
            account=organization.stripe_account_id,
            type="account_onboarding",
            return_url=return_url,
            refresh_url=refresh_url,
            api_key=settings.STRIPE_SECRET_KEY,
        )
    except stripe.error.StripeError as exc:
        logger.exception("Stripe Connect onboarding link failed for org %s", organization.pk)
        raise ConnectError(str(exc)) from exc
    return link.url


def refresh_account_status(organization):
    """Re-read the connected account from Stripe and cache its two capability
    flags (`charges_enabled`, `details_submitted`) onto the Organization.
    Called both from the onboarding return view (so the dashboard reflects
    completion immediately) and the `account.updated` webhook (so a theater
    that finishes/loses a requirement later stays current without staff
    action). No-op returning False if the org has no connected account yet."""
    if not organization.stripe_account_id:
        return False
    try:
        account = stripe.Account.retrieve(
            organization.stripe_account_id, api_key=settings.STRIPE_SECRET_KEY
        )
    except stripe.error.StripeError:
        logger.exception("Could not retrieve Stripe account for org %s", organization.pk)
        return False
    return apply_account_status(organization, account)


def apply_account_status(organization, account):
    """Persist the capability flags off a Stripe Account object (from a
    retrieve OR an account.updated webhook payload — both expose the same
    fields, dict- or attr-accessed). Writes only when something changed, so a
    stream of unrelated account.updated events doesn't churn the row."""
    charges_enabled = bool(_account_field(account, "charges_enabled"))
    details_submitted = bool(_account_field(account, "details_submitted"))
    if (
        organization.stripe_charges_enabled == charges_enabled
        and organization.stripe_details_submitted == details_submitted
    ):
        return charges_enabled
    organization.stripe_charges_enabled = charges_enabled
    organization.stripe_details_submitted = details_submitted
    organization.save(update_fields=["stripe_charges_enabled", "stripe_details_submitted"])
    return charges_enabled


def _account_field(account, name):
    # Stripe objects support attribute access; a raw webhook `data.object`
    # dict does not. Accept either so callers don't have to normalize first.
    if isinstance(account, dict):
        return account.get(name)
    return getattr(account, name, None)


# --- Platform fee --------------------------------------------------------


def application_fee_amount(organization, total, currency=None):
    """The platform's cut for an order whose total is `total` (Decimal major
    units), as integer minor units for Stripe's `application_fee_amount`, or
    None when there is no fee to charge.

    Rate = the org's `platform_fee_percent` override if set, else the global
    settings.PLATFORM_FEE_PERCENT, plus a flat settings.PLATFORM_FEE_FIXED_CENTS
    per order. Both default to 0 (see settings/base.py), so until a real rate
    is chosen this returns None and the Checkout Session is created WITHOUT an
    application fee — Stripe rejects an explicit fee of 0, and "no cut yet" is
    the intended launch behavior. Returning None (not 0) is what lets the
    caller omit the parameter entirely.

    `currency` (the charge currency, defaulting to the org's) picks the minor-
    unit exponent so the fee matches the charge: Stripe requires
    application_fee_amount in the SAME currency/unit as the PaymentIntent, so
    a JPY charge must not carry a cents-scaled (100x) fee. The fixed component
    is already in minor units and added as-is."""
    percent = organization.platform_fee_percent
    if percent is None:
        percent = settings.PLATFORM_FEE_PERCENT
    percent = Decimal(percent)
    fixed_minor = int(settings.PLATFORM_FEE_FIXED_CENTS)
    code = (currency or organization.currency or "").upper()

    fee_major = total * percent / Decimal(100)  # percentage fee, in major units
    if code in _ZERO_DECIMAL_CURRENCIES:
        pct_minor = int(fee_major.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    elif code in _THREE_DECIMAL_CURRENCIES:
        pct_minor = int((fee_major * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        pct_minor = (pct_minor // 10) * 10  # Stripe requires a multiple of 10 here.
    else:
        pct_minor = int((fee_major * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    fee_minor = pct_minor + fixed_minor
    if fee_minor <= 0:
        return None
    return fee_minor


# --- Checkout Session creation ------------------------------------------


def create_checkout_session(hold, request):
    """Create a Stripe Checkout Session for `hold` and return its hosted
    payment page URL. Does NOT create an Order/Payment/Ticket -- those are
    created by fulfill_checkout_session() once Stripe confirms payment via
    the checkout.session.completed webhook. `request` supplies the tenant
    host (for success/cancel URLs) and nothing else; it is never sent to
    Stripe.

    Direct charge: the session is created ON this theater's connected account
    (`stripe_account=org.stripe_account_id`) with the platform key, and the
    platform's cut is attached as `application_fee_amount` (omitted entirely
    when application_fee_amount() returns None — the launch default of no cut).

    STUB MODE: if this tenant can't take real payments yet — it hasn't
    finished Connect onboarding, so `stripe_charges_enabled` is False (no
    connected account, or one still in requirements) — a real Stripe call
    would fail. Instead we SIMULATE the hosted checkout: return the URL of an
    internal stub payment page (orders/views.py's checkout_stub) that fulfills
    the hold with a simulated payment (no money moves, no Stripe call of any
    kind). This keeps the browse -> buy -> ticket flow working end to end on a
    tenant that hasn't connected Stripe, which is the whole point of the demo/
    pre-launch setup. See checkout_stub for the "simulated payment" half.
    """
    if hold.expires_at <= timezone.now():
        raise CheckoutError("Your hold has expired. Please make your selection again.")

    line_items = _line_items_for_hold(hold)
    if not line_items:
        raise CheckoutError("There's nothing to check out.")

    organization = hold.organization

    if not organization.stripe_charges_enabled:
        logger.info(
            "Organization %s can't take payments yet (Connect charges not "
            "enabled); using the simulated checkout stub for hold %s instead "
            "of calling Stripe.",
            organization.pk,
            hold.pk,
        )
        return request.build_absolute_uri(reverse("checkout_stub")) + f"?hold_id={hold.pk}"

    success_url = request.build_absolute_uri(reverse("checkout_success")) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = request.build_absolute_uri(reverse("checkout_cancel"))

    params = {
        "mode": "payment",
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"hold_id": str(hold.pk), "organization_id": str(organization.pk)},
    }
    fee = application_fee_amount(
        organization, order_services.hold_total(hold), currency=_hold_currency(hold)
    )
    if fee is not None:
        # On a direct charge, the application fee is set on the underlying
        # PaymentIntent the Checkout Session creates.
        params["payment_intent_data"] = {"application_fee_amount": fee}

    session = stripe.checkout.Session.create(
        **params,
        api_key=settings.STRIPE_SECRET_KEY,
        stripe_account=organization.stripe_account_id,
    )
    return session.url


def _hold_currency(hold):
    """The single charge currency for `hold` (a Stripe session is one
    currency). GA reads the tier's currency; reserved reads the first
    hold_seat's tier currency, falling back to the org's currency for a
    zone-priced seat (zones carry no currency -- see _line_items_for_hold)."""
    if hold.price_tier_id:
        return hold.price_tier.currency
    first = hold.hold_seats.select_related("price_tier").first()
    if first is not None and first.price_tier_id:
        return first.price_tier.currency
    return hold.organization.currency


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
                    # Charge the GA price the buyer saw, snapshotted onto the
                    # hold -- not a live re-read of tier.amount (see
                    # order_services.ga_unit_amount / Hold.ga_unit_amount).
                    "unit_amount": _to_minor_units(order_services.ga_unit_amount(hold), tier.currency),
                    "product_data": {
                        "name": f"{hold.performance.event.title} — {tier.name}",
                    },
                },
            }
        ]

    line_items = []
    for hold_seat in hold.hold_seats.select_related("seat__section", "price_tier", "pricing_zone"):
        seat = hold_seat.seat
        # Phase C: hold_seat.unit_amount is the snapshot to charge regardless
        # of source (PricingZone or PriceTier -- see HoldSeat's docstring).
        # Currency isn't a PricingZone concept (only PriceTier has one), so
        # a zone-priced seat falls back to the organization's own currency.
        currency = hold_seat.price_tier.currency if hold_seat.price_tier_id else hold.organization.currency
        line_items.append(
            {
                "quantity": 1,
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": _to_minor_units(hold_seat.unit_amount, currency),
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


# Currencies Stripe expects in whole units, NOT cents (amount * 1, no minor
# unit). Passing e.g. a ¥500 charge as 50000 would overcharge 100x. Source:
# Stripe "zero-decimal currencies" list.
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF",
        "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)
# Currencies Stripe expects in thousandths, and which must be a multiple of 10
# minor units (it drops the last digit). Source: Stripe "three-decimal
# currencies" list.
_THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})


def _to_minor_units(amount, currency):
    """Decimal major units (e.g. dollars) -> integer minor units for Stripe,
    for the given ISO currency code. Most currencies are 2-decimal (cents),
    but zero-decimal currencies (JPY, KRW, ...) charge in whole units and
    three-decimal currencies (KWD, BHD, ...) in thousandths rounded to a
    multiple of 10 -- getting this wrong mis-charges the buyer by 100x, so the
    exponent is chosen by currency, not assumed. PriceTier.amount stores 2
    decimal places regardless, so `to_integral_value` truncates the sub-unit
    tail for a zero-decimal currency (a ¥500.00 tier -> 500)."""
    code = (currency or "").upper()
    if code in _ZERO_DECIMAL_CURRENCIES:
        return int(amount.to_integral_value())
    if code in _THREE_DECIMAL_CURRENCIES:
        minor = int((amount * 1000).to_integral_value())
        return (minor // 10) * 10  # Stripe requires a multiple of 10 here.
    return int((amount * 100).to_integral_value())


# --- Shared fulfillment core ---------------------------------------------


@transaction.atomic
def fulfill_hold(hold, *, buyer_email, buyer_name, payment_ref, provider, stripe_checkout_session_id=None):
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
    total = order_services.hold_total(hold)

    # Attach (or create) the buyer's per-theater guest account so this order
    # shows up in their self-service portal (guests/). Keyed off buyer_email;
    # a blank email (possible on a Stripe session that carried none) just
    # leaves guest=None -- see GuestAccountManager.get_or_create_for_email.
    # Done inside this same transaction so a fulfilled order and its guest
    # link commit atomically.
    guest, _ = GuestAccount.objects.get_or_create_for_email(
        organization, buyer_email, name=buyer_name
    )

    order = Order.objects.create(
        organization=organization,
        performance=hold.performance,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        guest=guest,
        total=total,
        status=Order.Status.PAID,
        stripe_checkout_session_id=stripe_checkout_session_id,
    )

    if hold.price_tier_id and hold.quantity:
        _fulfill_ga(organization, hold, order)
    else:
        _fulfill_reserved(organization, hold, order)

    Payment.objects.create(
        organization=organization,
        order=order,
        provider=provider,
        amount=total,
        status="succeeded",
        provider_ref=payment_ref,
    )

    hold.delete()  # HoldSeat rows cascade-delete with it.
    return order


# --- Webhook fulfillment (checkout.session.completed) --------------------


@transaction.atomic
def fulfill_checkout_session(organization, session):
    """Turn a paid Stripe Checkout Session into an Order + OrderItems +
    Tickets. Called by the webhook view after signature verification.

    Idempotent: if an Order already exists for `session["id"]` (a Stripe
    retry, or this handler running twice), returns that Order unchanged with
    created=False and does nothing else -- no duplicate Order/Tickets, no
    double-incrementing GAAllocation.sold. Otherwise, re-validates the Hold
    named in the session's metadata and hands off to fulfill_hold() (see its
    docstring for what "fulfill" means) with provider="stripe" and this
    session's id.

    Returns (order, created). Raises FulfillmentError (never touches the
    DB before the raise) if the Hold is gone/expired or availability no
    longer holds. Sending the ticket email is the caller's job, done AFTER
    this returns/commits -- email delivery shouldn't be able to roll back a
    paid order.
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
            f"the organization ({organization.pk}) resolved from the event's connected account."
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

    # The `existing` check above is correct in sequence but isn't itself
    # locked -- two truly concurrent deliveries for the same session_id can
    # both read "no Order yet" before either commits (see Order.Meta's
    # unique_stripe_checkout_session_per_org constraint docstring).
    # fulfill_hold()'s own @transaction.atomic gives its Order.objects.create()
    # call a savepoint, so a unique-constraint violation there can be caught
    # and handled below without poisoning this transaction; the loser falls
    # back to the winner's now-committed Order instead of raising 500 into
    # the webhook view (which would just make Stripe retry into the same
    # race again).
    try:
        order = fulfill_hold(
            hold,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            payment_ref=session.get("payment_intent") or session_id,
            provider="stripe",
            stripe_checkout_session_id=session_id,
        )
    except IntegrityError as exc:
        winner = Order.objects.filter(
            organization=organization, stripe_checkout_session_id=session_id
        ).first()
        if winner is not None:
            # The concurrent-duplicate-session race: the other delivery
            # committed the Order first and won the unique_stripe_checkout_
            # session_per_org constraint. Fall back to its Order.
            return winner, False
        # No committed winner, so this wasn't the dup-session race. The only
        # other IntegrityError fulfill_hold can raise is
        # unique_live_ticket_per_performance_seat firing because a seat got
        # ticketed concurrently after _fulfill_reserved's lock/recheck (very
        # narrow, but possible). Re-raise as the availability failure it is --
        # a FulfillmentError the webhook view acks with 200 -- rather than a
        # bare IntegrityError that 500s into a 3-day Stripe retry loop that
        # can never succeed. Chained so the original constraint error is still
        # in the logged traceback.
        raise AvailabilityChangedError(
            f"Fulfilling session {session_id} hit a database integrity conflict "
            f"(a seat was most likely ticketed concurrently); not retrying."
        ) from exc

    return order, True


# --- Refunds (staff-facing) ----------------------------------------------


@transaction.atomic
def refund_order(order):
    """Refund a PAID order in full, void its tickets, and mark it REFUNDED.

    Idempotent: an order that isn't currently PAID (already refunded,
    cancelled, or pending) is a no-op returning False, so a double-click or a
    retried request can't refund twice. For a real Stripe order, issues a
    Stripe Refund on the theater's connected account against the charge's
    PaymentIntent; for a stub/test order (provider != "stripe", no real
    charge) it records the reversal without calling Stripe. Voids every live
    ticket via order_services.void_order so a refunded ticket can't be
    admitted and its inventory is freed, then records a Payment row
    (status="refunded") for the audit trail. Returns True when a refund was
    performed.

    Raises RefundError (rolling back the whole transaction, so the order stays
    PAID and nothing is voided) if Stripe rejects the refund.
    """
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status != Order.Status.PAID:
        return False

    payment = order.payments.filter(status="succeeded").order_by("id").first()
    provider = payment.provider if payment is not None else "stripe"
    refund_ref = ""

    if provider == "stripe":
        payment_intent = payment.provider_ref if payment is not None else ""
        if not payment_intent.startswith("pi_"):
            # provider_ref falls back to the session id when a session carried
            # no payment_intent (rare); a Refund needs the PaymentIntent, so
            # send staff to the Stripe dashboard rather than guessing.
            raise RefundError(
                "This order has no Stripe PaymentIntent on file; refund it from the "
                "Stripe dashboard."
            )
        try:
            refund = stripe.Refund.create(
                payment_intent=payment_intent,
                api_key=settings.STRIPE_SECRET_KEY,
                stripe_account=order.organization.stripe_account_id,
            )
        except stripe.error.StripeError as exc:
            logger.exception("Stripe refund failed for order %s", order.pk)
            raise RefundError(str(exc)) from exc
        refund_ref = refund.id

    order_services.void_order(order)
    order.status = Order.Status.REFUNDED
    order.save(update_fields=["status"])
    Payment.objects.create(
        organization=order.organization,
        order=order,
        provider=provider,
        amount=order.total,
        status="refunded",
        provider_ref=refund_ref,
    )
    return True


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
