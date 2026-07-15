from django.contrib import admin

from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import StackedInline as UnfoldStackedInline
from unfold.admin import TabularInline as UnfoldTabularInline

from .models import Event, GAAllocation, Performance, PriceTier, PricingZone, ZoneTemplate


class PerformanceInline(UnfoldTabularInline):
    model = Performance
    extra = 0
    fields = ("venue", "starts_at", "seating_mode", "status")
    show_change_link = True


@admin.register(Event)
class EventAdmin(UnfoldModelAdmin):
    list_display = ("title", "organization", "status", "category", "created_at")
    list_filter = ("organization", "status", "category")
    search_fields = ("title", "slug", "description")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [PerformanceInline]


class GAAllocationInline(UnfoldStackedInline):
    model = GAAllocation
    extra = 0


@admin.register(Performance)
class PerformanceAdmin(UnfoldModelAdmin):
    list_display = ("event", "venue", "starts_at", "seating_mode", "status", "organization")
    list_filter = ("organization", "seating_mode", "status", "venue")
    search_fields = ("event__title",)
    inlines = [GAAllocationInline]


@admin.register(PriceTier)
class PriceTierAdmin(UnfoldModelAdmin):
    """Staff can set BOTH `performance` and `section` here to create a
    per-performance override (a higher/lower price for that section on one
    specific performance) -- the dashboard CRUD only ever creates the GA
    flat tier or the section's chart-wide default, so overrides are
    admin-only for now. See PriceTier's docstring / events/pricing.py for
    the resolution rule. `target` spells out which of the three shapes each
    row is so overrides are easy to spot in the list view."""

    list_display = ("name", "amount", "currency", "target", "performance", "section", "organization")
    list_filter = ("organization", "currency")
    search_fields = ("name", "performance__event__title", "section__name")

    @admin.display(description="Target")
    def target(self, obj):
        if obj.performance_id and obj.section_id:
            return "Override (section × performance)"
        if obj.performance_id:
            return "GA performance"
        if obj.section_id:
            return "Section default"
        return "—"


@admin.register(GAAllocation)
class GAAllocationAdmin(UnfoldModelAdmin):
    list_display = ("performance", "capacity", "sold", "organization")
    list_filter = ("organization",)


@admin.register(ZoneTemplate)
class ZoneTemplateAdmin(UnfoldModelAdmin):
    list_display = ("name", "color", "organization")
    list_filter = ("organization",)
    search_fields = ("name",)


@admin.register(PricingZone)
class PricingZoneAdmin(UnfoldModelAdmin):
    list_display = ("name", "amount", "color", "performance", "template", "organization")
    list_filter = ("organization", "performance")
    search_fields = ("name", "performance__event__title")
