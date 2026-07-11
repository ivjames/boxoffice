from django.db import models


class Organization(models.Model):
    """A theater/tenant. One row per branded storefront subdomain."""

    class InfraStatus(models.TextChoices):
        # DNS/nginx/TLS state for this tenant's <subdomain>.<BASE_DOMAIN>.
        # Creating the row (admin/CLI) is the DB half; these track the infra
        # half, driven by the admin "Provision infrastructure" action ->
        # `manage.py provision_pending_tenants` (root cron worker).
        NONE = "none", "Not provisioned"
        PENDING = "pending", "Queued"
        PROVISIONING = "provisioning", "Provisioning…"
        PROVISIONED = "provisioned", "Live"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    subdomain = models.SlugField(
        max_length=63,
        unique=True,
        help_text="The subdomain this tenant is served on, e.g. 'roxy' for roxy.boxo.show.",
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

    # Infrastructure (DNS + nginx vhost + TLS) provisioning state for this
    # tenant's subdomain. Set to PENDING by the admin action; advanced by the
    # `provision_pending_tenants` cron worker (see tenants/admin.py and that
    # command). infra_message holds the worker's last note/error for the admin.
    # db_default (not just default) so a row inserted without these columns —
    # e.g. via a historical model in a migration that predates this field —
    # still gets a value, instead of tripping the NOT NULL constraint.
    infra_status = models.CharField(
        max_length=20,
        choices=InfraStatus.choices,
        default=InfraStatus.NONE,
        db_default=InfraStatus.NONE,
    )
    infra_message = models.TextField(blank=True, default="", db_default="")

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
