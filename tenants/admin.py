from zoneinfo import available_timezones

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path

from .models import Organization

# Sorted list of IANA zones for the timezone dropdown — replaces the free-text
# field that let a typo like "Amerca/New_York" silently fall back to UTC
# (see TenantMiddleware._activate_timezone).
_TZ_CHOICES = [(tz, tz) for tz in sorted(available_timezones())]

# A small, ISO-4217 set covering the currencies these theaters actually price
# in. Kept as a fixed list (not free text) so `currency` can't drift to an
# invalid code; extend as new markets come online.
_CURRENCY_CHOICES = [
    ("USD", "USD — US Dollar"),
    ("CAD", "CAD — Canadian Dollar"),
    ("EUR", "EUR — Euro"),
    ("GBP", "GBP — British Pound"),
    ("AUD", "AUD — Australian Dollar"),
    ("NZD", "NZD — New Zealand Dollar"),
]


class OrganizationAdminForm(forms.ModelForm):
    """Admin form for Organization with sensible widgets:

    - Stripe secret + webhook secret are WRITE-ONLY: rendered as empty password
      inputs (render_value=False so the stored secret never reaches the HTML),
      and a blank submit KEEPS the current value instead of wiping it. Only
      typing a new value overwrites it. The publishable key (pk_…) is public,
      so it stays a normal text field.
    - Colors use a native color picker (<input type="color">).
    - Timezone and currency are ChoiceFields, so the value is validated
      server-side against the allowed set — not just rendered as a dropdown.
      (A bare Select widget only styles the input; the underlying CharField
      would still accept an arbitrary posted value like `Amerca/New_York`.)
    """

    # Declared as fields (not just widgets) so `full_clean` rejects any value
    # outside the list, whatever posts it — a tampered request, an admin
    # script, `save()` from the shell. Renders as a <select> either way.
    timezone = forms.ChoiceField(choices=_TZ_CHOICES)
    currency = forms.ChoiceField(choices=_CURRENCY_CHOICES)

    class Meta:
        model = Organization
        fields = "__all__"
        widgets = {
            "stripe_secret_key": forms.PasswordInput(render_value=False),
            "stripe_webhook_secret": forms.PasswordInput(render_value=False),
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "accent_color": forms.TextInput(attrs={"type": "color"}),
        }
        help_texts = {
            "stripe_secret_key": "Write-only. Leave blank to keep the current secret.",
            "stripe_webhook_secret": "Write-only. Leave blank to keep the current secret.",
        }

    def _keep_current_if_blank(self, field):
        # A blank secret field means "unchanged" (the value is never rendered
        # back into the form), so preserve whatever is already stored rather
        # than overwriting a live key with "".
        submitted = self.cleaned_data.get(field)
        return submitted if submitted else getattr(self.instance, field, "")

    def clean_stripe_secret_key(self):
        return self._keep_current_if_blank("stripe_secret_key")

    def clean_stripe_webhook_secret(self):
        return self._keep_current_if_blank("stripe_webhook_secret")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    form = OrganizationAdminForm
    change_form_template = "admin/tenants/organization/change_form.html"

    list_display = ("name", "subdomain", "is_active", "infra_status", "currency", "created_at")
    list_filter = ("is_active", "infra_status")
    search_fields = ("name", "slug", "subdomain", "contact_email")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("infra_status", "infra_message", "created_at", "updated_at")
    actions = ("provision_infrastructure",)

    fieldsets = (
        (None, {"fields": ("name", "slug", "subdomain", "contact_email", "is_active")}),
        ("Branding", {"fields": ("logo", "primary_color", "accent_color")}),
        ("Localization", {"fields": ("timezone", "currency")}),
        (
            "Stripe",
            {
                "fields": (
                    "stripe_publishable_key",
                    "stripe_secret_key",
                    "stripe_webhook_secret",
                ),
                "description": (
                    "Per-tenant Stripe credentials. Secret fields are write-only — "
                    "leave blank to keep the current value."
                ),
            },
        ),
        ("Infrastructure", {"fields": ("infra_status", "infra_message")}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    # --- Per-object provisioning button (change form) --------------------

    def get_urls(self):
        # A "Provision infrastructure" button on a single tenant's change form,
        # so you don't have to go back to the changelist and use the bulk
        # action just to (re)provision the row you're already looking at.
        custom = [
            path(
                "<path:object_id>/provision/",
                self.admin_site.admin_view(self.provision_one_view),
                name="tenants_organization_provision",
            ),
        ]
        return custom + super().get_urls()

    def provision_one_view(self, request, object_id):
        org = self.get_object(request, object_id)
        if org is None or not self.has_change_permission(request, org):
            self.message_user(request, "Organization not found.", messages.ERROR)
            return redirect("admin:tenants_organization_changelist")
        if request.method == "POST":
            # Reuse the exact queueing logic (and messaging) of the bulk action.
            self.provision_infrastructure(request, Organization.objects.filter(pk=org.pk))
        return redirect("admin:tenants_organization_change", object_id)

    @admin.action(description="Provision infrastructure (DNS + nginx + TLS)")
    def provision_infrastructure(self, request, queryset):
        """(Re)provision infrastructure for the selected tenants. This only
        flips infra_status to PENDING (a cheap DB write — the web process never
        touches DNS/nginx/certbot); the root `provision_pending_tenants` cron
        worker picks them up within a minute and runs the actual idempotent
        `add-tenant --infra-only` flow, writing the result back to
        infra_status/infra_message.

        Queueable from any state, so this doubles as retry (FAILED), repair
        (re-run on a live PROVISIONED tenant), and recovery (a PROVISIONING row
        stranded by a crashed worker). Only rows already sitting in the queue
        (PENDING) are skipped — re-queuing them would be a no-op."""
        In = Organization.InfraStatus
        # Count already-queued rows BEFORE the update — the update flips the
        # others to PENDING, so a lazy queryset re-checked afterwards would
        # count them too.
        already_queued = queryset.filter(infra_status=In.PENDING).count()
        queued = queryset.exclude(infra_status=In.PENDING).update(
            infra_status=In.PENDING, infra_message="Queued from admin."
        )

        if queued:
            self.message_user(
                request,
                f"Queued {queued} tenant(s) for provisioning — the worker runs "
                "every minute; watch the Infra status column.",
                messages.SUCCESS,
            )
        if already_queued:
            self.message_user(
                request,
                f"Skipped {already_queued} already queued.",
                messages.WARNING,
            )
