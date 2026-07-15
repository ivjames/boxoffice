from django.contrib import admin

from unfold.admin import ModelAdmin as UnfoldModelAdmin

from .models import PromoCode


@admin.register(PromoCode)
class PromoCodeAdmin(UnfoldModelAdmin):
    list_display = (
        "code",
        "organization",
        "kind",
        "value",
        "is_active",
        "redemption_count",
        "max_redemptions",
        "starts_at",
        "ends_at",
    )
    list_filter = ("organization", "kind", "is_active")
    search_fields = ("code",)
    # redemption_count is bumped only by the fulfillment path
    # (promotions.services.record_redemption); surfacing it read-only keeps an
    # admin from hand-editing the usage tally out from under that accounting.
    readonly_fields = ("redemption_count", "created_at")
