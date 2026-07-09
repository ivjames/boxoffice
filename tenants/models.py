from django.db import models


class Organization(models.Model):
    """A theater/tenant. One row per branded storefront subdomain."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    subdomain = models.SlugField(
        max_length=63,
        unique=True,
        help_text="The subdomain this tenant is served on, e.g. 'roxy' for roxy.lab980.com.",
    )

    logo = models.ImageField(upload_to="org_logos/", blank=True, null=True)
    primary_color = models.CharField(
        max_length=7, default="#111111", help_text="Hex color, e.g. #111111."
    )
    accent_color = models.CharField(
        max_length=7, default="#e11d48", help_text="Hex color, e.g. #e11d48."
    )

    timezone = models.CharField(max_length=63, default="UTC")
    currency = models.CharField(max_length=3, default="USD")

    # Per-tenant Stripe Connect-style credentials. Each theater brings its own
    # Stripe account; these are used (in a later phase) to create Checkout
    # Sessions and verify webhook signatures for that tenant only.
    stripe_publishable_key = models.CharField(max_length=255, blank=True)
    stripe_secret_key = models.CharField(max_length=255, blank=True)
    stripe_webhook_secret = models.CharField(max_length=255, blank=True)

    contact_email = models.EmailField()
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TenantScopedManager(models.Manager):
    """
    Base manager for tenant-scoped models. Does NOT auto-filter by tenant
    (there's no ambient "current tenant" at the manager level) — its purpose
    is to be the single place later apps hang tenant-aware query helpers off
    of, e.g. `for_organization(org)`. Views/queries are still responsible for
    always filtering by `request.organization`; see TenantScopedModel below
    and the "Tenant isolation is non-negotiable" rule in ARCHITECTURE.md.
    """

    def for_organization(self, organization):
        return self.get_queryset().filter(organization=organization)


class TenantScopedModel(models.Model):
    """
    Abstract base for every tenant-owned model (venues, events, orders, ...).
    Adds the mandatory `organization` FK + an index covering the common
    "everything for this tenant" query pattern. Subclasses should add
    `(organization, <their own lookup fields>)` indexes as needed.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="%(class)ss",
    )

    objects = TenantScopedManager()

    class Meta:
        abstract = True
        indexes = [models.Index(fields=["organization"])]
