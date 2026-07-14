from django.contrib import admin

from .models import CampaignSend, EmailCampaign


@admin.register(EmailCampaign)
class EmailCampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "segment_kind",
        "status",
        "recipient_count",
        "created_at",
        "sent_at",
    )
    list_filter = ("organization", "status", "segment_kind")
    search_fields = ("name", "subject")
    readonly_fields = ("created_at", "updated_at", "sent_at", "recipient_count")


@admin.register(CampaignSend)
class CampaignSendAdmin(admin.ModelAdmin):
    list_display = ("email", "campaign", "organization", "status", "created_at", "sent_at")
    list_filter = ("organization", "status")
    search_fields = ("email",)
    readonly_fields = ("created_at", "sent_at")
