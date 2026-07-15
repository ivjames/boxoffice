"""Template-only bridge from storefront templates into passes' entitlement
predicates -- so orders/views.py and orders/services.py never need to import
this app at the Python level (see passes.context_processors' dependency-
direction note). templates/base.html loads this for the redeem-mode banner's
remaining-admissions label; templates/orders/cart.html and checkout.html load
it to decide, per hold, whether to show the "Redeem with pass" CTA.

Every tag here is purely advisory/display logic -- the authoritative re-check
of every entitlement fact happens inside payments.services.fulfill_hold_with_pass
under a row lock at actual redemption time (see its docstring). A UI-only
false positive here just means a redeem attempt bounces back with a buyer-safe
error; it can never over-grant an admission.
"""

from django import template

from passes.models import PassProduct
from passes.services import pass_covers_performance, remaining_admissions

register = template.Library()


@register.simple_tag
def redeemable_with_pass(hold, pass_purchase):
    """Would `pass_purchase` plausibly cover `hold`'s seats right now? False
    whenever `pass_purchase` is None (i.e. not in redeem mode), so callers
    don't need to guard that case separately -- `{% redeemable_with_pass
    item.hold redeeming_pass as x %}` is safe to call unconditionally."""
    if pass_purchase is None or hold is None:
        return False
    if not pass_covers_performance(pass_purchase, hold.performance):
        return False
    quantity = hold.quantity if (hold.price_tier_id and hold.quantity) else hold.hold_seats.count()
    if quantity <= 0:
        return False
    if pass_purchase.kind == PassProduct.Kind.FLEX:
        remaining = remaining_admissions(pass_purchase)
        return remaining is None or quantity <= remaining
    # Season: one admission per covered event -- a hold of more than one seat
    # can never be redeemed against a season pass in a single redemption (see
    # payments.services.fulfill_hold_with_pass's authoritative check).
    return quantity == 1


@register.simple_tag
def pass_remaining_label(pass_purchase):
    """Human label for how much `pass_purchase` has left -- "N credit(s)
    remaining" (flex), "N show(s) remaining" (bounded season), or "unlimited
    shows" (an all-access season pass -- see remaining_admissions' None
    case). Used by the redeem-mode banner (templates/base.html)."""
    if pass_purchase is None:
        return ""
    remaining = remaining_admissions(pass_purchase)
    if remaining is None:
        return "unlimited shows"
    if pass_purchase.kind == PassProduct.Kind.FLEX:
        noun = "credit"
    else:
        noun = "show"
    plural = "" if remaining == 1 else "s"
    return f"{remaining} {noun}{plural} remaining"
