from django.contrib import admin

from .models import PassProduct, PassPurchase, PassRedemption


@admin.register(PassProduct)
class PassProductAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "kind", "price", "credit_count", "is_active", "created_at")
    list_filter = ("organization", "kind", "is_active")
    search_fields = ("name",)
    readonly_fields = ("created_at",)
    filter_horizontal = ("events",)


@admin.register(PassPurchase)
class PassPurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "product", "kind", "status", "credits_remaining", "created_at")
    list_filter = ("organization", "kind", "status")
    search_fields = ("order__buyer_email", "guest__email")
    readonly_fields = ("created_at",)
    raw_id_fields = ("product", "guest", "order")
    filter_horizontal = ("covered_events",)


@admin.register(PassRedemption)
class PassRedemptionAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "pass_purchase", "event", "performance", "credits_used", "face_value", "created_at")
    list_filter = ("organization",)
    readonly_fields = ("created_at",)
    raw_id_fields = ("pass_purchase", "order", "ticket", "performance", "event", "seat")
