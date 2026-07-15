from django.contrib import admin

from unfold.admin import ModelAdmin as UnfoldModelAdmin

from .models import DonationCampaign


@admin.register(DonationCampaign)
class DonationCampaignAdmin(UnfoldModelAdmin):
    list_display = ("name", "organization", "is_active", "suggested_amounts", "created_at")
    list_filter = ("organization", "is_active")
    search_fields = ("name",)
    readonly_fields = ("created_at",)
