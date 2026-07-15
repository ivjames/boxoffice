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

    # Stripe Connect (Express) — the platform (boxo.show) is the Stripe
    # account of record; each theater is a CONNECTED account it onboards.
    # We store only the connected account id (acct_…) plus a cached copy of
    # the two capability flags Stripe reports for it; there are no per-tenant
    # secret keys anymore (the platform key in settings.STRIPE_SECRET_KEY is
    # used for every call, with `stripe_account=<this id>` selecting the
    # connected account for direct charges). `charges_enabled` gates whether
    # this theater can actually sell yet — kept fresh by the `account.updated`
    # Connect webhook and by the onboarding return view. See payments/services.py.
    stripe_account_id = models.CharField(max_length=255, blank=True)
    stripe_charges_enabled = models.BooleanField(default=False, db_default=False)
    stripe_details_submitted = models.BooleanField(default=False, db_default=False)

    # Optional per-org override of the platform take rate (percent of order
    # total). NULL falls back to settings.PLATFORM_FEE_PERCENT — the global
    # default. Lets a negotiated theater run a different rate without a code
    # change. See payments.services.application_fee_amount.
    platform_fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Override platform fee % for this theater. Blank = use the global default.",
    )

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

    @property
    def base_url(self):
        """The absolute origin (scheme + host, no trailing slash) of this
        tenant's storefront -- e.g. "https://roxy.boxo.show".

        Rebuilt from the subdomain + settings.BASE_DOMAIN rather than any
        request, because the flows that need it often have NO request on the
        right host: the campaign cron sender has no request at all, and the
        Stripe Connect webhook's request is for the PLATFORM host
        (boxo.show/webhooks/stripe/ -- see DEPLOY.md), not the theater's
        subdomain, so request.build_absolute_uri there mints links that 404
        on tenant-gated routes. Uses http only when BASE_DOMAIN is a local
        dev host (localhost / 127.*) -- everywhere else the storefront is
        HTTPS (per-site certbot, see DEPLOY.md)."""
        from django.conf import settings

        base_domain = settings.BASE_DOMAIN or "localhost"
        scheme = "http" if base_domain.startswith(("localhost", "127.")) else "https"
        return f"{scheme}://{self.subdomain}.{base_domain}"


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


class ContactInquiry(models.Model):
    """A "Get in touch" submission from the platform landing page's contact
    form. Deliberately platform-level (NOT TenantScopedModel): these are
    prospective venues writing to Boxo.show itself, before any Organization
    exists for them.

    The DB row is the source of truth -- the form works (and leads are never
    lost) even while outbound mail is unconfigured. A courtesy email
    notification is layered on top only when delivery actually works; see
    tenants/emails.py and DEPLOY.md's "Mail" section.
    """

    name = models.CharField(max_length=120)
    email = models.EmailField()
    venue = models.CharField(
        max_length=200,
        blank=True,
        help_text="The venue/theater the sender is asking about (optional).",
    )
    message = models.TextField(max_length=5000)

    # Triage flag for /admin: flipped by the "Mark handled" action once
    # someone has replied. Submission fields above stay read-only there.
    is_handled = models.BooleanField(default=False, db_default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "contact inquiries"

    def __str__(self):
        return f"{self.name} <{self.email}>"
