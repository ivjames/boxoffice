from django.core.files.uploadedfile import UploadedFile
from django.core.validators import RegexValidator
from django.db import models
from django.utils.text import slugify

from .color_schemes import COLOR_ROLES, HEX_COLOR_RE, ROLE_TO_ORG_FIELD
from .fonts import DEFAULT_BODY_FONT, DEFAULT_HEADING_FONT, font_stack, google_families
from .logo_images import process_logo_file, validate_logo_upload

# Shared validator for every stored hex color -- keeps the model, the admin,
# and the dashboard branding form agreeing on what "a color" is.
validate_hex_color = RegexValidator(
    HEX_COLOR_RE, "Enter a hex color like #4B2E83 or #abc."
)


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

    class PageTint(models.TextChoices):
        # How much brand presence the storefront page background carries. The
        # page tint is DERIVED from the scheme's brand hue at this intensity
        # (tenants.color_generator.page_background), kept above WCAG AAA for body
        # text. "none" is the untinted near-white (the original look).
        NONE = "none", "None — clean white"
        SUBTLE = "subtle", "Subtle — a hint of brand color"
        MEDIUM = "medium", "Medium — clearly branded"
        BOLD = "bold", "Bold — saturated ground"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    subdomain = models.SlugField(
        max_length=63,
        unique=True,
        help_text="The subdomain this tenant is served on, e.g. 'roxy' for roxy.boxo.show.",
    )

    logo = models.ImageField(
        upload_to="org_logos/",
        blank=True,
        null=True,
        validators=[validate_logo_upload],
        help_text="Shown in your storefront header, emails, browser tab, and "
        "social-share cards. Large images are automatically resized.",
    )

    # Six-role brand palette (see tenants/color_schemes.py for the role model).
    # `primary_color` and `accent_color` predate the six-role scheme and stay
    # the load-bearing pair app.css keys on -- `accent_color` IS the Feature
    # Accent highlight/CTA role. The other four were added with the scheme and
    # are exposed as extra CSS variables in templates/base.html. Applying a
    # ColorScheme copies its six roles onto these fields (see apply_color_scheme).
    primary_color = models.CharField(
        max_length=7, default="#111111", validators=[validate_hex_color],
        help_text="Primary brand color. Hex, e.g. #111111.",
    )
    accent_color = models.CharField(
        max_length=7, default="#e11d48", validators=[validate_hex_color],
        help_text="Feature Accent highlight (buttons, links). Hex, e.g. #e11d48.",
    )
    secondary_color = models.CharField(
        max_length=7, default="#374151", validators=[validate_hex_color],
        help_text="Secondary brand color. Hex, e.g. #374151.",
    )
    dark_accent_color = models.CharField(
        max_length=7, default="#0e0e12", validators=[validate_hex_color],
        help_text="Deep shade for dark sections. Hex, e.g. #0e0e12.",
    )
    light_neutral_color = models.CharField(
        max_length=7, default="#f5f5f4", validators=[validate_hex_color],
        help_text="Light neutral background. Hex, e.g. #f5f5f4.",
    )
    neutral_color = models.CharField(
        max_length=7, default="#111111", validators=[validate_hex_color],
        help_text="Near-black neutral (body text). Hex, e.g. #111111.",
    )
    # Storefront page-background presence. The background is derived from the
    # brand hue at this intensity (Organization.page_background_color); "subtle"
    # gives a gentle brand tint out of the box, tenants can dial it up or down.
    page_tint = models.CharField(
        max_length=7,
        choices=PageTint.choices,
        default=PageTint.SUBTLE,
        help_text="How much brand color the storefront page background carries.",
    )

    # Storefront typography (see tenants/fonts.py). Stored as catalog font
    # keys, resolved to CSS font-family stacks in templates/base.html
    # (--heading-font / --body-font). Defaults are the system stacks the
    # storefront already used, so existing tenants render unchanged.
    heading_font = models.CharField(max_length=40, default=DEFAULT_HEADING_FONT)
    body_font = models.CharField(max_length=40, default=DEFAULT_BODY_FONT)

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

    def save(self, *args, **kwargs):
        # Normalize a freshly-uploaded logo (downscale + optimized PNG) at the
        # one choke point every writer passes through -- the dashboard branding
        # form, Django admin, and the background-removal endpoint alike. The
        # UploadedFile check keeps this to NEW uploads: a logo already stored in
        # media is a plain FieldFile here and is left untouched, so re-saving an
        # org for any other reason never re-encodes (and degrades) its logo.
        if self.logo and isinstance(getattr(self.logo, "file", None), UploadedFile):
            process_logo_file(self.logo)
        super().save(*args, **kwargs)

    @property
    def palette(self):
        """The org's six brand colors keyed by role -- what base.html emits as
        CSS variables. Reads straight off the color fields via the role→field
        mapping, so it always reflects the currently-applied colors whether
        they came from a preset, a saved custom scheme, or a manual edit."""
        return {
            role: getattr(self, field) for role, field in ROLE_TO_ORG_FIELD.items()
        }

    @property
    def on_colors(self):
        """The legible text color to use over each themed fill (best-of-two:
        dark `neutral` on a light fill, light `light_neutral` on a dark one) --
        emitted as --on-* CSS variables in base.html so the storefront's text on
        colored surfaces is WCAG-accessible for whatever scheme is applied. See
        tenants.color_generator.text_over."""
        from .color_generator import text_over

        ln, n = self.light_neutral_color, self.neutral_color
        return {
            "primary": text_over(self.primary_color, ln, n),
            "secondary": text_over(self.secondary_color, ln, n),
            "accent": text_over(self.accent_color, ln, n),
            "dark_accent": text_over(self.dark_accent_color, ln, n),
        }

    @property
    def page_background_color(self):
        """The storefront page background (emitted as --bg-color in base.html).
        Derived from the brand hue at the tenant's `page_tint` intensity and
        kept above WCAG AAA for body text; "none" is the untinted light_neutral
        (the original near-white). See color_generator.page_background."""
        from .color_generator import page_background

        return page_background(self.palette, self.page_tint)

    @property
    def ink_colors(self):
        """Legible 'ink' versions of the brand colors for use AS TEXT
        (headings, links) on the PAGE background -- a pale brand color is
        darkened to the same hue so it stays readable, while fills keep the
        exact brand color. Computed against page_background_color (not raw
        light_neutral) so ink stays legible even when the page is tinted.
        Emitted as --primary-ink/--accent-ink/--secondary-ink in base.html.
        See color_generator.readable_on."""
        from .color_generator import readable_on

        bg = self.page_background_color
        return {
            "primary": readable_on(self.primary_color, bg),
            "accent": readable_on(self.accent_color, bg),
            "secondary": readable_on(self.secondary_color, bg),
        }

    @property
    def dark_colors(self):
        """The storefront's dark-theme values, emitted as CSS variables in
        base.html under prefers-color-scheme / [data-theme]. The brand FILLS and
        their on-colors are shared with the light theme (a button stays the same
        brand color); what changes is the page: a branded near-black background,
        light text, and brand 'ink' colors lightened so headings/links stay
        legible on the dark page. See tenants.color_generator.dark_surfaces."""
        from .color_generator import dark_surfaces, dark_ink

        surfaces = dark_surfaces(self.palette)
        bg = surfaces["bg"]
        return {
            **surfaces,
            "primary_ink": dark_ink(self.primary_color, bg),
            "accent_ink": dark_ink(self.accent_color, bg),
            "secondary_ink": dark_ink(self.secondary_color, bg),
        }

    @property
    def heading_font_stack(self):
        """The CSS font-family stack for the org's heading font (base.html)."""
        return font_stack(self.heading_font)

    @property
    def body_font_stack(self):
        """The CSS font-family stack for the org's body font (base.html)."""
        return font_stack(self.body_font)

    @property
    def google_font_families(self):
        """The Google Fonts `family=` specs to load for this org's two fonts
        (empty for system-stack picks), so base.html requests exactly what's
        used and nothing more."""
        return google_families(self.heading_font, self.body_font)

    def apply_color_scheme(self, scheme, *, commit=True):
        """Copy a ColorScheme's six roles onto this org's color fields. Returns
        the list of field names touched (handy for a targeted save()). The
        storefront reads the org's own fields, never the scheme, so a later
        edit to the source scheme never silently re-themes a tenant."""
        fields = []
        for role, field in ROLE_TO_ORG_FIELD.items():
            setattr(self, field, getattr(scheme, role))
            fields.append(field)
        if commit:
            self.save(update_fields=fields)
        return fields

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


class ColorScheme(models.Model):
    """A named six-role palette (see tenants/color_schemes.py). Two flavors,
    distinguished by `organization`:

    - Built-in presets (`organization` NULL, `is_preset` True): the shared,
      tenant-agnostic catalog seeded from BUILTIN_SCHEMES. Every tenant sees
      these in the branding picker; none can edit or delete them.
    - Custom schemes (`organization` set): a single tenant's saved palettes --
      created by hand in the branding page or captured from the
      derive-from-homepage agent. Only that tenant sees them.

    A scheme is a *template*: applying it copies its colors onto the org's own
    fields (Organization.apply_color_scheme). It is never the live source the
    storefront renders from, so editing a scheme never re-themes a tenant that
    applied it earlier.
    """

    # Role fields, one per tenants.color_schemes.COLOR_ROLES key. Kept as
    # explicit columns (not a JSON blob) so they validate, admin-edit, and
    # query like any other color field.
    primary = models.CharField(max_length=7, validators=[validate_hex_color])
    secondary = models.CharField(max_length=7, validators=[validate_hex_color])
    feature_accent = models.CharField(max_length=7, validators=[validate_hex_color])
    dark_accent = models.CharField(max_length=7, validators=[validate_hex_color])
    light_neutral = models.CharField(max_length=7, validators=[validate_hex_color])
    neutral = models.CharField(max_length=7, validators=[validate_hex_color])

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, blank=True)

    # NULL == a built-in preset shared across all tenants; set == this tenant's
    # own saved scheme. on_delete=CASCADE so deleting a tenant takes its custom
    # schemes with it (presets have no org, so they're untouched).
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="color_schemes",
        null=True,
        blank=True,
    )
    is_preset = models.BooleanField(default=False, db_default=False)

    # Provenance for schemes the derive-from-homepage agent produced: the URL
    # it read. Blank for hand-made and built-in schemes.
    source_url = models.URLField(blank=True, default="")

    ordering = models.PositiveIntegerField(default=0, db_default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ordering", "name"]
        indexes = [models.Index(fields=["organization"])]
        constraints = [
            # A tenant can't have two custom schemes with the same slug; presets
            # (organization NULL) are globally unique by slug. Two partial
            # uniques rather than one (organization, slug) so NULL-org rows are
            # still deduped (NULLs don't collide under a plain unique_together).
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(organization__isnull=True),
                name="unique_preset_scheme_slug",
            ),
            models.UniqueConstraint(
                fields=["organization", "slug"],
                condition=models.Q(organization__isnull=False),
                name="unique_custom_scheme_slug_per_org",
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:140]
        super().save(*args, **kwargs)

    @property
    def roles(self):
        """The scheme's six colors keyed by role -- the shape the branding UI,
        swatches, and the derive agent all speak in."""
        return {role: getattr(self, role) for role, _label, _field in COLOR_ROLES}


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
