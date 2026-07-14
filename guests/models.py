from django.db import models

from tenants.models import TenantScopedModel


class GuestAccountManager(models.Manager):
    """Manager for the tenant-scoped GuestAccount. Adds `for_organization`
    (mirroring TenantScopedManager) plus `get_or_create_for_email`, the
    single place that normalizes a buyer's email into a GuestAccount so the
    checkout path and the self-service portal agree on what "the same guest"
    means (case-insensitive, whitespace-trimmed)."""

    def for_organization(self, organization):
        return self.get_queryset().filter(organization=organization)

    def get_or_create_for_email(self, organization, email, *, name=""):
        """Return (guest, created) for (organization, normalized email).

        Email is the identity key, so it's normalized the same way on the
        way in from every path (checkout fulfillment, portal sign-in): lower-
        cased and stripped. A blank email yields (None, False) -- callers
        must treat a guest as optional (an Order can be fulfilled from a
        Stripe session that somehow carried no email, and staff-issued comps
        may have none)."""
        normalized = normalize_email(email)
        if not normalized:
            return None, False
        guest, created = self.get_or_create(
            organization=organization,
            email=normalized,
            defaults={"name": name.strip()},
        )
        # Backfill a name onto a guest first seen without one, so a later
        # purchase that does carry a name isn't silently dropped.
        if not created and name.strip() and not guest.name:
            guest.name = name.strip()
            guest.save(update_fields=["name"])
        return guest, created


def normalize_email(email):
    """Canonical form used as the GuestAccount identity key: trimmed +
    lower-cased. Kept as a module function (not just a manager method) so the
    portal's sign-in view can normalize a typed-in email the exact same way
    before looking one up."""
    return (email or "").strip().lower()


class GuestAccount(TenantScopedModel):
    """A ticket buyer's lightweight, per-theater account, keyed by email.

    Deliberately NOT an accounts.User: staff Users log in with a password and
    carry Memberships/roles, whereas a guest is a member of the public who
    bought tickets and just needs to come back and see them. Guests never set
    a password -- they prove ownership of the email by clicking a signed,
    expiring magic link (guests.tokens), so there's no credential to store or
    leak here. Scoped to one Organization (like every storefront concept):
    the same person buying at two theaters is two GuestAccounts, one per
    subdomain, exactly as their Orders are tenant-scoped.

    Orders link here via Order.guest (nullable) at fulfillment time
    (payments.services.fulfill_hold), which is what lets the portal list
    "all your orders" instead of one-order-per-unguessable-token."""

    email = models.EmailField()
    name = models.CharField(max_length=255, blank=True)

    # --- Phase 4 CRM / email-marketing fields --------------------------------
    # All four are ADDITIVE (no new index/constraint): a GuestAccount already
    # exists per (org, email) and is linked to every order at fulfillment, so
    # these just annotate that anchor with the consent + light CRM metadata the
    # campaigns app segments and sends against. LTV / order-count stay COMPUTED
    # queries (campaigns.services.audience_queryset annotates them off Orders) --
    # deliberately not stored columns, so they can never drift from the orders
    # they summarize. See docs/ROADMAP.md Phase 4.
    #
    # marketing_opt_in is the single gate every bulk send checks (the sender
    # re-reads it at send time, campaigns.services.segment_guests filters on it):
    # consent is a legal prerequisite, so it defaults False and is only ever
    # flipped True by an explicit act (checkout opt-in tickbox, the portal
    # toggle). db_default mirrors marketing_opt_in's Python default so a row
    # inserted by a historical migration model (which doesn't know this column)
    # still satisfies NOT NULL -- the same reason Organization's flags carry one.
    marketing_opt_in = models.BooleanField(default=False, db_default=False)
    # When consent was FIRST given (stamped once by guests.services.
    # record_marketing_opt_in / set_marketing_opt_in). Retained even after an
    # unsubscribe (set_marketing_opt_in flips the bool False but leaves this
    # standing) so there's an audit trail of when the guest had opted in.
    marketing_opt_in_at = models.DateTimeField(null=True, blank=True)
    # Free-form CSV of staff-applied labels ("vip,subscriber,board") the
    # audience list filters on. Parsed leniently by tag_list (below), exactly
    # like DonationCampaign.suggested_amounts -- presentation/segmentation only,
    # never a money-path input.
    tags = models.CharField(max_length=255, blank=True, default="")
    # Private staff notes about this guest (never shown on the storefront or in
    # any email). Purely for the dashboard's audience detail.
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = GuestAccountManager()

    class Meta(TenantScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email"], name="unique_guest_email_per_org"
            ),
        ]
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "email"]),
        ]

    def __str__(self):
        return f"{self.email} @ {self.organization_id}"

    def display_name(self):
        return self.name or self.email

    def tag_list(self):
        """The `tags` CSV parsed into a de-duped, order-preserving list of
        trimmed non-empty labels, so a fat-fingered "vip, , subscriber,vip"
        yields ["vip", "subscriber"] rather than blanks or repeats. Lenient by
        design -- the exact mirror of DonationCampaign.suggested_amount_list:
        tags are a presentation/segmentation convenience (the audience filter
        matches against them), never an authoritative input, so a stray comma
        or duplicate is skipped rather than surfaced as an error. Case is
        preserved (labels are shown back to staff verbatim); only exact repeats
        are dropped, first occurrence winning to keep the staff-entered order."""
        seen = set()
        labels = []
        for chunk in (self.tags or "").split(","):
            chunk = chunk.strip()
            if not chunk or chunk in seen:
                continue
            seen.add(chunk)
            labels.append(chunk)
        return labels
