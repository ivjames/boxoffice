from django.contrib import admin

from .models import Event, GAAllocation, Performance, PriceTier


class PerformanceInline(admin.TabularInline):
    model = Performance
    extra = 0
    fields = ("venue", "starts_at", "seating_mode", "status")
    show_change_link = True


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "organization", "status", "category", "created_at")
    list_filter = ("organization", "status", "category")
    search_fields = ("title", "slug", "description")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [PerformanceInline]


class GAAllocationInline(admin.StackedInline):
    model = GAAllocation
    extra = 0


@admin.register(Performance)
class PerformanceAdmin(admin.ModelAdmin):
    list_display = ("event", "venue", "starts_at", "seating_mode", "status", "organization")
    list_filter = ("organization", "seating_mode", "status", "venue")
    search_fields = ("event__title",)
    inlines = [GAAllocationInline]


@admin.register(PriceTier)
class PriceTierAdmin(admin.ModelAdmin):
    list_display = ("name", "amount", "currency", "performance", "section", "organization")
    list_filter = ("organization", "currency")
    search_fields = ("name",)


@admin.register(GAAllocation)
class GAAllocationAdmin(admin.ModelAdmin):
    list_display = ("performance", "capacity", "sold", "organization")
    list_filter = ("organization",)
