"""Storefront context processor: whether passes are switched on for the
current tenant, and which pass (if any) the current session is mid-redeeming
-- so templates/base.html can show a "Passes" nav link + a redeem-mode
banner without every view computing this separately (mirrors donations.
context_processors.donation_nav).

DEPENDENCY-DIRECTION NOTE: orders/views.py and orders/services.py must NOT
import this app (see passes.services' module docstring -- the money-path-
agnostic stance). This context processor is how the storefront's cart/
checkout pages get at `redeeming_pass` without orders importing passes: it's
wired into every template render via config.settings' TEMPLATES setting, not
via an orders import. The per-item "does this pass cover this hold" check
that cart.html/checkout.html need is the same story, one level down --
see passes/templatetags/pass_tags.py, loaded directly by those templates.
"""

from .models import PassProduct, PassPurchase
from .services import redeemable_now

# Session key holding the PassPurchase pk the current session is redeeming
# against (set by passes.views.pass_redeem_start, cleared by pass_redeem_exit
# and by a successful pass_redeem, and swept for staleness right here on
# every request).
REDEEMING_PASS_SESSION_KEY = "redeeming_pass_id"


def pass_nav(request):
    """`{"passes_enabled": bool, "redeeming_pass": PassPurchase|None}`.

    `passes_enabled` mirrors donation_nav's own read-only stance: a plain
    filter().exists() (never a get-or-create), so rendering a page never has
    the side effect of switching passes "on" for an org that has none.

    `redeeming_pass` resolves the session's redeeming_pass_id, org-scoped, and
    self-heals a STALE session key (the pass was refunded/exhausted/expired
    since redemption mode started, or belongs to a different org's session
    somehow) by popping it -- so a dead session key can't leave the redeem-
    mode banner stuck on forever. Kept intentionally simple per the v1 spec:
    an org-scoped lookup + a liveness check, not a guest-ownership re-check on
    every single page view (pass_redeem itself re-checks ownership + every
    other entitlement fact authoritatively at redemption time)."""
    organization = getattr(request, "organization", None)
    if organization is None:
        return {"passes_enabled": False, "redeeming_pass": None}

    passes_enabled = PassProduct.objects.filter(
        organization=organization, is_active=True
    ).exists()

    redeeming_pass = None
    pass_id = request.session.get(REDEEMING_PASS_SESSION_KEY)
    if pass_id:
        redeeming_pass = PassPurchase.objects.filter(
            organization=organization, pk=pass_id
        ).first()
        if redeeming_pass is None or not redeemable_now(redeeming_pass):
            request.session.pop(REDEEMING_PASS_SESSION_KEY, None)
            redeeming_pass = None

    return {"passes_enabled": passes_enabled, "redeeming_pass": redeeming_pass}
