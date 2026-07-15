from decimal import Decimal, InvalidOperation

from django.db import models

from tenants.models import TenantScopedModel


class DonationCampaign(TenantScopedModel):
    """A named fund a buyer can give to -- the provenance a donation
    OrderItem points back at (OrderItem.donation_campaign) and the carrier
    for the nonprofit acknowledgment text a receipt reprints.

    v1 SCOPING DECISION -- one implicit campaign per org. Every theater gets
    a single "general fund" campaign, created lazily on first need by
    donations.services.get_or_create_general_fund; the storefront's "add a
    donation" add-on and the standalone /donate/ page both give to it. There
    is deliberately no per-event / multi-campaign UI yet -- a theater can't
    run two simultaneous appeals ("building fund" vs "youth program") in v1.
    That was a conscious "ship the money path first" call, not an oversight,
    and it's cheap to widen later: the FK from OrderItem/Hold already carries
    which campaign a gift was for, so multi-campaign is a dashboard-CRUD +
    selection-UI change, with no money-path or fulfillment code to touch (a
    donation's amount is snapshotted on the line item, exactly like a
    ticket's -- see below).

    `is_active` doubles as the per-org donations ENABLE flag: a theater that
    hasn't turned donations on has no active campaign, so the storefront
    hides the add-on and /donate/ 404s. Retiring donations is flipping this
    False, never deleting the row (past donation OrderItems SET_NULL their FK
    if it ever were deleted, but the row is kept for the report/audit trail).
    """

    name = models.CharField(max_length=255, default="General Fund")
    # Nonprofit blurb reprinted on the receipt/confirmation for a gift to
    # this campaign (e.g. "The Roxy is a 501(c)(3); no goods or services were
    # provided in exchange for this contribution. EIN 12-3456789."). Blank =
    # no acknowledgment line. Presentation-only; carries no money-path
    # meaning.
    acknowledgment = models.TextField(blank=True, default="")
    # CSV of suggested give amounts the storefront renders as preset buttons
    # (e.g. "10,25,50,100"). PRESENTATION-ONLY -- a buyer may give any amount;
    # these are just quick-pick shortcuts, and the authoritative charge is
    # whatever amount is validated at add-to-cart time, never re-read from
    # here. Parsed leniently by suggested_amount_list (below), so a stray
    # blank / non-numeric / non-positive entry is skipped rather than 500-ing
    # the storefront.
    suggested_amounts = models.CharField(max_length=255, default="10,25,50,100")
    # Doubles as the donations enable flag for the org -- see class docstring.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            # The hot lookup is "this org's active campaign(s)" --
            # get_or_create_general_fund and the storefront both filter on
            # (organization, is_active).
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.organization})"

    def suggested_amount_list(self):
        """The `suggested_amounts` CSV parsed into a list of positive Decimals
        for the storefront's preset buttons, skipping any blank, non-numeric,
        or non-positive entry (so a fat-fingered "10,,abc,-5,25" yields
        [Decimal('10'), Decimal('25')] rather than raising). Presentation-only
        -- these are quick-pick shortcuts, never the authoritative charge (see
        the field comment); the amount actually charged is validated fresh at
        add-to-cart / donate time."""
        amounts = []
        for chunk in (self.suggested_amounts or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                value = Decimal(chunk)
            except (InvalidOperation, ValueError):
                continue
            if value > 0:
                amounts.append(value)
        return amounts
