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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

import stripe

from donations.models import DonationCampaign
from events.models import GAAllocation
from guests import services as guest_services
from guests.models import GuestAccount
from orders import services as order_services
from orders.models import Hold, Order, OrderItem, Payment, Ticket
from passes import services as passes_services
from passes.models import PassProduct, PassPurchase, PassRedemption
from promotions import services as promotions_services
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


def create_checkout_session(hold, request, *, marketing_opt_in=False):
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

    # Resolve the charge currency once -- both the platform fee and the promo
    # coupon below must be denominated in it.
    currency = _hold_currency(hold)

    params = {
        "mode": "payment",
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"hold_id": str(hold.pk), "organization_id": str(organization.pk)},
    }
    # Phase 4: carry the buyer's marketing consent through Stripe so fulfillment
    # can honor it (read back as `metadata.get("marketing_opt_in") == "1"` in
    # fulfill_checkout_session, threaded into fulfill_hold). Added ONLY when the
    # box was ticked so the default (no consent) leaves metadata byte-identical
    # to the pre-Phase-4 shape -- an absent key reads as False, so this stays
    # regression-safe for every existing caller.
    if marketing_opt_in:
        params["metadata"]["marketing_opt_in"] = "1"
    # The platform's cut is a percentage of what's ACTUALLY collected, so the
    # fee base is the NET (post-discount) grand total, not the gross subtotal --
    # otherwise a discounted order would over-charge the fee against money that
    # never changed hands.
    fee = application_fee_amount(
        organization, order_services.hold_grand_total(hold), currency=currency
    )
    if fee is not None:
        # On a direct charge, the application fee is set on the underlying
        # PaymentIntent the Checkout Session creates.
        params["payment_intent_data"] = {"application_fee_amount": fee}

    # Apply the promo discount as a Stripe COUPON rather than by shrinking the
    # line items. The line items stay GROSS (list price) so the hosted page and
    # the buyer's receipt show the discount as its own explicit deduction --
    # matching how the cart/checkout summary presents it. Reconciliation is
    # exact because every unit price is 2-dp: gross_minor - amount_off =
    # net_minor = the Order.total (hold_grand_total) fulfillment records.
    #
    # Three deliberate choices here:
    #  - the coupon is created ON THE CONNECTED ACCOUNT (stripe_account=...), the
    #    same direct-charge account the Session lives on -- a platform-account
    #    coupon can't be attached to a connected-account Checkout Session;
    #  - duration="once" so even if this Session is abandoned, the orphaned
    #    coupon is inert (it only ever discounts a single charge, and only if
    #    redeemed) -- no cleanup job needed;
    #  - the coupon is minted fresh per Session (not reused) so its amount_off
    #    always matches THIS hold's snapshotted discount exactly.
    discount_minor = _to_minor_units(order_services.hold_discount(hold), currency)
    if discount_minor > 0:
        coupon = stripe.Coupon.create(
            amount_off=discount_minor,
            currency=currency.lower(),
            duration="once",
            name=(hold.promo_code_text or "Discount")[:40],
            api_key=settings.STRIPE_SECRET_KEY,
            stripe_account=organization.stripe_account_id,
        )
        params["discounts"] = [{"coupon": coupon.id}]

    session = stripe.checkout.Session.create(
        **params,
        api_key=settings.STRIPE_SECRET_KEY,
        stripe_account=organization.stripe_account_id,
    )
    return session.url


def create_donation_checkout_session(organization, *, amount, campaign, buyer_email, buyer_name, request, marketing_opt_in=False):
    """Create a Stripe Checkout Session for a STANDALONE donation (the
    /donate/ page -- a gift with no hold, no tickets) and return its hosted
    payment page URL. The donation analogue of create_checkout_session: one
    donation line item, the same direct-charge shape (session created ON the
    connected account with the platform key, the platform's cut attached as
    application_fee_amount), and the same success/cancel URLs. Does NOT create
    an Order -- fulfill_checkout_session does that on the
    checkout.session.completed webhook, branching on metadata["kind"] ==
    "donation" to call fulfill_donation.

    The session carries everything fulfillment needs on the Order in metadata
    (there's no Hold to look up, unlike the ticket path): kind="donation", the
    org id (tenant re-check), the campaign id (provenance -- looked up org-
    scoped at fulfillment, tolerated as None if the campaign was deleted
    meanwhile), and the amount as a string (the authoritative figure, parsed
    back to Decimal at fulfillment). customer_email pre-fills the buyer's email
    on the hosted page when we have one.

    STUB MODE (charges not enabled -- same pre-launch case as the ticket path):
    a real Stripe call would fail, so return an internal stub URL instead. The
    named `donate_stub` route lands with the donations UI layer (donations/
    urls.py); reverse() is called lazily inside this branch only, so this
    function is importable/usable on the charges-enabled path before that URL
    exists."""
    currency = (organization.currency or "usd").lower()

    if not organization.stripe_charges_enabled:
        logger.info(
            "Organization %s can't take payments yet (Connect charges not "
            "enabled); using the simulated donation stub instead of calling "
            "Stripe.",
            organization.pk,
        )
        # `donate_stub` is added by the donations UI layer (donations/urls.py);
        # resolved lazily here so the charges-enabled path doesn't need it.
        return request.build_absolute_uri(reverse("donate_stub")) + f"?amount={amount}"

    success_url = request.build_absolute_uri(reverse("checkout_success")) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = request.build_absolute_uri(reverse("checkout_cancel"))

    line_items = [
        {
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": _to_minor_units(amount, currency),
                "product_data": {"name": f"Donation — {organization.name}"},
            },
        }
    ]

    params = {
        "mode": "payment",
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "kind": "donation",
            "organization_id": str(organization.pk),
            "donation_campaign_id": str(campaign.pk) if campaign is not None else "",
            "donation_amount": str(amount),
        },
    }
    # Phase 4 marketing consent -- added only when ticked (regression-safe absent
    # default). See create_checkout_session.
    if marketing_opt_in:
        params["metadata"]["marketing_opt_in"] = "1"
    if buyer_email:
        params["customer_email"] = buyer_email

    # The platform's cut is a percentage of the gift actually collected.
    fee = application_fee_amount(organization, amount, currency=currency)
    if fee is not None:
        params["payment_intent_data"] = {"application_fee_amount": fee}

    session = stripe.checkout.Session.create(
        **params,
        api_key=settings.STRIPE_SECRET_KEY,
        stripe_account=organization.stripe_account_id,
    )
    return session.url


def create_pass_checkout_session(organization, *, product, buyer_email, buyer_name, request, marketing_opt_in=False):
    """Create a Stripe Checkout Session for a one-time PASS purchase (a season
    or flex pass bought outright -- see docs/ROADMAP.md Phase 3's "one-time
    purchase, not a Stripe subscription" decision) and return its hosted payment
    page URL. The pass analogue of create_donation_checkout_session: one line
    item at product.price, the same direct-charge shape (session created ON the
    connected account with the platform key, the platform's cut attached as
    application_fee_amount), and the same success/cancel URLs. Does NOT create
    an Order -- fulfill_checkout_session does that on the
    checkout.session.completed webhook, branching on metadata["kind"] == "pass"
    to call fulfill_pass_purchase.

    The session carries everything fulfillment needs in metadata (there's no
    Hold, like the donation path): kind="pass", the org id (tenant re-check),
    and the pass product id (looked up org-scoped at fulfillment; a product
    deleted meanwhile is a PassDataError -- unlike a donation campaign, we can't
    fulfill a pass whose terms are gone). customer_email pre-fills the buyer's
    email on the hosted page.

    STUB MODE (charges not enabled -- same pre-launch case as the ticket/
    donation paths): a real Stripe call would fail, so return an internal stub
    URL instead. The named `pass_stub` route lands with the passes UI layer
    (passes/urls.py); reverse() is called lazily inside this branch only, so
    this function is importable/usable on the charges-enabled path before that
    URL exists."""
    currency = (organization.currency or "usd").lower()

    if not organization.stripe_charges_enabled:
        logger.info(
            "Organization %s can't take payments yet (Connect charges not "
            "enabled); using the simulated pass stub instead of calling Stripe.",
            organization.pk,
        )
        # `pass_stub` is added by the passes UI layer (passes/urls.py); resolved
        # lazily here so the charges-enabled path doesn't need it.
        return request.build_absolute_uri(reverse("pass_stub")) + f"?product_id={product.pk}"

    success_url = request.build_absolute_uri(reverse("checkout_success")) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = request.build_absolute_uri(reverse("checkout_cancel"))

    line_items = [
        {
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": _to_minor_units(product.price, currency),
                "product_data": {"name": f"{product.name} — {organization.name}"},
            },
        }
    ]

    params = {
        "mode": "payment",
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "kind": "pass",
            "organization_id": str(organization.pk),
            "pass_product_id": str(product.pk),
        },
    }
    # Phase 4 marketing consent -- added only when ticked (regression-safe absent
    # default). See create_checkout_session.
    if marketing_opt_in:
        params["metadata"]["marketing_opt_in"] = "1"
    if buyer_email:
        params["customer_email"] = buyer_email

    # The platform's cut is a percentage of the pass price actually collected.
    fee = application_fee_amount(organization, product.price, currency=currency)
    if fee is not None:
        params["payment_intent_data"] = {"application_fee_amount": fee}

    session = stripe.checkout.Session.create(
        **params,
        api_key=settings.STRIPE_SECRET_KEY,
        stripe_account=organization.stripe_account_id,
    )
    return session.url


def _hold_currency(hold):
    """The single charge currency for `hold`. Now a thin delegate to
    orders.services.hold_currency -- the definition moved there so the promo
    path (which must resolve the charge currency to validate a fixed-amount
    code) and this Stripe path read ONE implementation. Kept as a payments-side
    name so the existing call sites here don't churn; payments imports
    orders.services, so this direction is safe (orders never imports payments)."""
    return order_services.hold_currency(hold)


def _line_items_for_hold(hold):
    """GA: a single line item, quantity x price_tier.amount. Reserved: one
    line item PER HoldSeat at its own price_tier.amount (seats can span
    sections/tiers within one hold, so they can't be collapsed into one
    line). Ad-hoc `price_data` is used instead of pre-created Stripe Price
    objects since tiers are defined and priced entirely on our side.

    Phase 2 donation add-on: BOTH paths append one extra donation line when
    the hold carries a gift (order_services.hold_donation > 0). The donation
    line is GROSS -- it is added at full value and is deliberately NOT
    discounted by the Stripe coupon. That reconciles exactly because the
    coupon's amount_off is bounded by the TICKET subtotal (validate_code
    forbids a discount reaching/exceeding hold_total, which excludes the
    donation -- see order_services.hold_grand_total), so a `duration="once"`
    coupon applied across the whole session can never reach past the ticket
    lines into the donation. Sum of these line items == hold_grand_total ==
    Order.total, gross-ticket + donation, minus the coupon = the exact net
    charge."""
    if hold.price_tier_id and hold.quantity:
        tier = hold.price_tier
        line_items = [
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
        _append_donation_line_item(hold, line_items)
        return line_items

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
    _append_donation_line_item(hold, line_items)
    return line_items


def _append_donation_line_item(hold, line_items):
    """Append the hold's donation to `line_items` in place when there's one to
    add (hold_donation > 0). Shared by both branches of _line_items_for_hold so
    the GA early-return and the reserved path add an identical GROSS donation
    line (see that function's docstring for why the donation stays gross). The
    donation is denominated in the hold's single charge currency (the same one
    the coupon and platform fee use)."""
    donation = order_services.hold_donation(hold)
    if donation <= Decimal("0.00"):
        return
    currency = order_services.hold_currency(hold)
    line_items.append(
        {
            "quantity": 1,
            "price_data": {
                "currency": currency.lower(),
                "unit_amount": _to_minor_units(donation, currency),
                "product_data": {
                    "name": f"Donation — {hold.organization.name}",
                },
            },
        }
    )


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
    # Phase 4: the buyer's marketing consent rode along in metadata (set by every
    # create_*_checkout_session). Decode it ONCE here and thread it into whichever
    # fulfill_* this session dispatches to. "" / absent -> False, so a session
    # created before this field existed (or with the box un-ticked) simply
    # doesn't opt anyone in.
    opt_in = metadata.get("marketing_opt_in") == "1"
    if session_org_id and str(session_org_id) != str(organization.pk):
        raise TenantMismatchError(
            f"Session {session_id} metadata organization_id={session_org_id} does not match "
            f"the organization ({organization.pk}) resolved from the event's connected account."
        )

    customer_details = session.get("customer_details") or {}
    buyer_email = customer_details.get("email") or ""
    buyer_name = customer_details.get("name") or ""

    # DONATION path: a standalone gift carries no Hold -- everything
    # fulfillment needs is in metadata (see create_donation_checkout_session).
    # Branch here, after the idempotency + tenant-mismatch checks (which apply
    # identically), and hand off to fulfill_donation inside the SAME
    # IntegrityError->winner fallback the hold path uses, so two concurrent
    # deliveries of one donation session collide on Order.objects.create()
    # rather than double-fulfilling.
    if metadata.get("kind") == "donation":
        try:
            amount = Decimal(metadata["donation_amount"])
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise DonationDataError(
                f"Donation session {session_id} has a missing/malformed donation_amount "
                f"({metadata.get('donation_amount')!r}); payment succeeded but nothing was "
                "fulfilled."
            ) from exc
        # Provenance only -- a campaign deleted between checkout and this
        # webhook resolves to None (the gift still fulfills; the OrderItem
        # simply carries no campaign FK, same SET_NULL stance as a live one).
        campaign_id = metadata.get("donation_campaign_id") or None
        campaign = None
        if campaign_id:
            campaign = DonationCampaign.objects.filter(
                organization=organization, pk=campaign_id
            ).first()
        try:
            order = fulfill_donation(
                organization,
                amount=amount,
                campaign=campaign,
                buyer_email=buyer_email,
                buyer_name=buyer_name,
                payment_ref=session.get("payment_intent") or session_id,
                provider="stripe",
                stripe_checkout_session_id=session_id,
                marketing_opt_in=opt_in,
            )
        except IntegrityError as exc:
            winner = Order.objects.filter(
                organization=organization, stripe_checkout_session_id=session_id
            ).first()
            if winner is not None:
                return winner, False
            raise
        return order, True

    # PASS purchase path: a one-time pass sale carries no Hold either --
    # everything fulfillment needs is in metadata (see
    # create_pass_checkout_session). Same shape as the donation branch: look the
    # product up ORG-SCOPED (a missing/deleted product is a PassDataError -- we
    # can't issue a pass whose terms are gone, unlike a donation), then hand off
    # to fulfill_pass_purchase inside the SAME IntegrityError->winner fallback so
    # two concurrent deliveries collide on Order.objects.create().
    if metadata.get("kind") == "pass":
        product_id = metadata.get("pass_product_id") or None
        product = None
        if product_id:
            product = PassProduct.objects.filter(
                organization=organization, pk=product_id
            ).first()
        if product is None:
            raise PassDataError(
                f"Pass session {session_id} names product {product_id!r} which is missing "
                "for this org; payment succeeded but nothing was fulfilled."
            )
        try:
            order = fulfill_pass_purchase(
                organization,
                product=product,
                buyer_email=buyer_email,
                buyer_name=buyer_name,
                payment_ref=session.get("payment_intent") or session_id,
                provider="stripe",
                stripe_checkout_session_id=session_id,
                marketing_opt_in=opt_in,
            )
        except IntegrityError as exc:
            winner = Order.objects.filter(
                organization=organization, stripe_checkout_session_id=session_id
            ).first()
            if winner is not None:
                return winner, False
            raise
        return order, True

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
            marketing_opt_in=opt_in,
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

    # PASS PURCHASE guard: if this order SOLD a pass (a kind=PASS line), block
    # the refund while that pass has been USED -- refunding a spent pass would
    # claw back money for admissions already handed out. Staff must void the
    # redeemed tickets first (which deletes those redemptions and frees the
    # entitlement -- see fulfill_hold_with_pass / restore_redemptions_for_order),
    # then the purchase can be refunded. Checked BEFORE any money moves so the
    # order stays PAID on rejection.
    sold_a_pass = order.items.filter(kind=OrderItem.Kind.PASS).exists()
    if sold_a_pass:
        for purchase in order.pass_purchases.all():
            if purchase.redemptions.exists():
                raise RefundError(
                    "This pass has been used; void its redeemed tickets first."
                )

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

    # Pass entitlement reversal, both directions:
    #  - If this was a pass REDEMPTION order (it USED a pass to comp its
    #    tickets), give the entitlement back: void_order above already voided the
    #    tickets; this deletes their PassRedemptions and restores flex credits /
    #    frees season slots. No-op for a non-redemption order.
    #  - If this was a pass PURCHASE order (it SOLD a pass), mark the now-refunded
    #    passes REFUNDED so they can't be redeemed going forward. (We already
    #    verified above none of them have redemptions.)
    passes_services.restore_redemptions_for_order(order)
    if sold_a_pass:
        order.pass_purchases.update(status=PassPurchase.Status.REFUNDED)

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
