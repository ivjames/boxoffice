"""Donation-campaign service layer.

v1 keeps a SINGLE implicit campaign per org (see DonationCampaign's
docstring): every theater gives to one "general fund", created lazily the
first time anything needs it. This module is the one place that
resolves/creates that campaign, so the storefront add-on, the standalone
/donate/ page (UI layer, added later), and the dashboard report all agree on
"the org's donation campaign" without each re-implementing the get-or-create.

DEPENDENCY DIRECTION: like promotions, this app stays money-path-agnostic --
it must NOT import from orders or payments. The money path (payments.services
fulfill_donation / fulfill_hold) depends on donations, never the reverse.
"""

from .models import DonationCampaign


def get_or_create_general_fund(organization):
    """Return `organization`'s single v1 donation campaign, creating a default
    "General Fund" (is_active=True) on first call and reusing it thereafter.

    v1 ONE-CAMPAIGN STANCE: a theater has exactly one campaign in v1 (see
    DonationCampaign's docstring), so "get the org's campaign" is "get its
    oldest campaign, or make one." Ordered by created_at (then pk as a
    stable tiebreaker) so that if a later multi-campaign version ever creates
    additional rows, this keeps resolving to the original general fund rather
    than picking one arbitrarily. Idempotent for the common single-row case.

    Note this CREATES an active campaign as a side effect -- callers that only
    want to know "does this org have donations turned on" should query for an
    active campaign directly rather than calling this (which would switch them
    on). The creation path here is for the flows that are explicitly setting a
    gift up (the /donate/ page, the checkout add-on)."""
    campaign = (
        DonationCampaign.objects.filter(organization=organization)
        .order_by("created_at", "pk")
        .first()
    )
    if campaign is not None:
        return campaign
    return DonationCampaign.objects.create(organization=organization)
