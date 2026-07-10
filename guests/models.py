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
