from django.contrib import admin, messages

from .models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "subdomain", "is_active", "infra_status", "currency", "created_at")
    list_filter = ("is_active", "infra_status")
    search_fields = ("name", "slug", "subdomain", "contact_email")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("infra_status", "infra_message", "created_at", "updated_at")
    actions = ("provision_infrastructure",)

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
