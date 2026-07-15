"""Storefront context processor: whether donations are switched on for the
current tenant, so templates/base.html can show a "Donate" nav link and
cart.html/checkout templates can gate their donation add-on without every
view having to compute it separately.
"""

from .models import DonationCampaign


def donation_nav(request):
    """`{"donations_enabled": bool}` for every template.

    False outside a tenant context (the platform host, request.organization
    is None) -- no DB hit in that case. Otherwise: does this org have an
    ACTIVE campaign right now. Deliberately a plain filter().exists() query,
    NOT donations.services.get_or_create_general_fund -- that helper CREATES
    a campaign as a side effect (see its docstring), and this runs on every
    single page view, so calling it here would silently switch donations on
    for every org the first time anyone loads a page. "Does an active
    campaign exist" is read-only and answers the real question ("are
    donations enabled") without that side effect.
    """
    organization = getattr(request, "organization", None)
    if organization is None:
        return {"donations_enabled": False}
    enabled = DonationCampaign.objects.filter(
        organization=organization, is_active=True
    ).exists()
    return {"donations_enabled": enabled}
