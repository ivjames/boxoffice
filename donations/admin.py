from django.contrib import admin

from .models import DonationCampaign


@admin.register(DonationCampaign)
class DonationCampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "is_active", "suggested_amounts", "created_at")
    list_filter = ("organization", "is_active")
    search_fields = ("name",)
    readonly_fields = ("created_at",)
