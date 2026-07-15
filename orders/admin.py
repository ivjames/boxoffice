from django.contrib import admin

from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import StackedInline as UnfoldStackedInline
from unfold.admin import TabularInline as UnfoldTabularInline

from .models import Hold, HoldSeat, Order, OrderItem, Payment, PerformanceSeatBlock, Ticket


class HoldSeatInline(UnfoldTabularInline):
    model = HoldSeat
    extra = 0


@admin.register(Hold)
class HoldAdmin(UnfoldModelAdmin):
    list_display = ("id", "performance", "session_key", "user", "quantity", "expires_at", "organization")
    list_filter = ("organization", "performance")
    search_fields = ("session_key", "user__email")
    inlines = [HoldSeatInline]


class OrderItemInline(UnfoldTabularInline):
    model = OrderItem
    extra = 0


class TicketInline(UnfoldTabularInline):
    model = Ticket
    extra = 0
    fields = ("token", "seat", "holder_name", "status", "used_at")
    readonly_fields = ("token",)


@admin.register(Order)
class OrderAdmin(UnfoldModelAdmin):
    list_display = ("token", "performance", "buyer_email", "total", "status", "created_at", "organization")
    list_filter = ("organization", "status")
    search_fields = ("token", "buyer_email", "buyer_name", "stripe_checkout_session_id")
    readonly_fields = ("token",)
    inlines = [OrderItemInline, TicketInline]


@admin.register(Ticket)
class TicketAdmin(UnfoldModelAdmin):
    list_display = ("token", "order", "performance", "seat", "status", "holder_name", "organization")
    list_filter = ("organization", "status", "performance")
    search_fields = ("token", "holder_name", "order__buyer_email")
    readonly_fields = ("token",)


@admin.register(Payment)
class PaymentAdmin(UnfoldModelAdmin):
    list_display = ("order", "provider", "amount", "status", "provider_ref")
    list_filter = ("organization", "provider", "status")
    search_fields = ("provider_ref", "order__token")


@admin.register(PerformanceSeatBlock)
class PerformanceSeatBlockAdmin(UnfoldModelAdmin):
    list_display = ("performance", "seat", "reason", "created_at", "organization")
    list_filter = ("organization", "performance")
    search_fields = ("reason",)
