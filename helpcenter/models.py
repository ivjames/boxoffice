"""Tenant-authored help / knowledge-base articles.

Each theater writes its own help content (house rules, show info, box-office
policies, how-tos) and tags every article with a `visibility` that decides
which audience may read it — from `PUBLIC` (ticket buyers on the storefront)
down through the staff role hierarchy (owner > manager > box_office >
scanner, mirrored from accounts.models.Membership). A short set of built-in
articles ships alongside these as a fallback so the Help section is useful
before a manager has written anything — those live in helpcenter/builtins.py,
NOT the database.

Visibility is cumulative in the same direction roles are: a `MANAGER`-visible
article is readable by managers AND owners; a `BOX_OFFICE` one by box office,
managers and owners; a `STAFF` one by any staff role; a `PUBLIC` one by
everyone including anonymous buyers. `readable_by()` (staff) and `public()`
(storefront) below are the single places that filter turns into a queryset,
so no view re-encodes the ordering.
"""

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from tenants.models import TenantScopedManager, TenantScopedModel


def visibilities_readable_by(membership):
    """The set of HelpArticle.Visibility values a staff Membership may read.

    Public + all-staff articles are visible to every role; the role-gated
    tiers accrue upward using Membership's own cumulative role helpers, so
    this never re-spells the owner>manager>box_office>scanner ordering.
    """
    allowed = {HelpArticle.Visibility.PUBLIC, HelpArticle.Visibility.STAFF}
    if membership.is_box_office_or_above():
        allowed.add(HelpArticle.Visibility.BOX_OFFICE)
    if membership.is_manager_or_above():
        allowed.add(HelpArticle.Visibility.MANAGER)
    if membership.is_owner():
        allowed.add(HelpArticle.Visibility.OWNER)
    return allowed


class HelpArticleManager(TenantScopedManager):
    def readable_by(self, organization, membership):
        """Published articles in `organization` visible to `membership`'s role."""
        return (
            self.for_organization(organization)
            .filter(is_published=True, visibility__in=visibilities_readable_by(membership))
            .order_by("category", "position", "title")
        )

    def public(self, organization):
        """Published, PUBLIC-visibility articles for the storefront FAQ."""
        return (
            self.for_organization(organization)
            .filter(is_published=True, visibility=HelpArticle.Visibility.PUBLIC)
            .order_by("category", "position", "title")
        )


class HelpArticle(TenantScopedModel):
    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public — ticket buyers & everyone"
        STAFF = "staff", "All staff"
        BOX_OFFICE = "box_office", "Box office & up"
        MANAGER = "manager", "Managers & owners"
        OWNER = "owner", "Owners only"

    class Category(models.TextChoices):
        GENERAL = "general", "General"
        VENUE_RULES = "venue_rules", "Venue rules"
        SHOW_INFO = "show_info", "Show information"
        POLICIES = "policies", "Policies"
        HOW_TO = "how_to", "Using Boxo"

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, blank=True)
    summary = models.CharField(
        max_length=280,
        blank=True,
        help_text="One-line teaser shown under the title in the article list.",
    )
    body = models.TextField(
        help_text="Plain text. Blank lines start a new paragraph; links are auto-detected.",
    )
    category = models.CharField(
        max_length=20, choices=Category.choices, default=Category.GENERAL
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.STAFF,
        help_text="Who can read this. 'Public' also shows it on the storefront FAQ.",
    )
    is_published = models.BooleanField(
        default=True,
        help_text="Unpublished drafts are only visible on the Manage screen.",
    )
    position = models.PositiveIntegerField(
        default=0, help_text="Lower numbers sort first within a category."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="help_articles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HelpArticleManager()

    # Flag mirrored by builtins.BuiltinArticle so a template can render a
    # DB-backed article and a shipped default through the same partial and
    # only offer edit/delete controls on the real (editable) ones.
    is_builtin = False

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "visibility"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="unique_help_slug_per_org"
            )
        ]
        ordering = ["category", "position", "title"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug()
        super().save(*args, **kwargs)

    def _unique_slug(self):
        """Derive a slug from the title, unique within this organization.

        slug uniqueness is scoped to (organization, slug) by the constraint
        above, so we only have to disambiguate against this tenant's own
        articles — never globally.
        """
        base = slugify(self.title) or "article"
        candidate = base
        n = 2
        siblings = HelpArticle.objects.filter(organization=self.organization)
        if self.pk:
            siblings = siblings.exclude(pk=self.pk)
        while siblings.filter(slug=candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate
