from django.contrib import admin

from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import StackedInline as UnfoldStackedInline
from unfold.admin import TabularInline as UnfoldTabularInline

from .models import ChartParseJob, Seat, SeatingChart, Section, Venue


class SeatInline(UnfoldTabularInline):
    model = Seat
    extra = 0
    fields = ("row_label", "number", "x", "y", "is_accessible")


class SectionInline(UnfoldTabularInline):
    model = Section
    extra = 0
    fields = ("name", "ordering")
    show_change_link = True


@admin.register(Venue)
class VenueAdmin(UnfoldModelAdmin):
    list_display = ("name", "organization", "timezone", "address")
    list_filter = ("organization",)
    search_fields = ("name", "address")


@admin.register(SeatingChart)
class SeatingChartAdmin(UnfoldModelAdmin):
    list_display = ("name", "venue", "organization")
    list_filter = ("organization", "venue")
    search_fields = ("name", "venue__name")
    inlines = [SectionInline]


@admin.register(Section)
class SectionAdmin(UnfoldModelAdmin):
    list_display = ("name", "chart", "ordering", "organization")
    list_filter = ("organization", "chart")
    search_fields = ("name",)
    inlines = [SeatInline]


@admin.register(Seat)
class SeatAdmin(UnfoldModelAdmin):
    list_display = ("__str__", "section", "is_accessible", "organization")
    list_filter = ("organization", "is_accessible", "section")
    search_fields = ("row_label", "number")


@admin.register(ChartParseJob)
class ChartParseJobAdmin(UnfoldModelAdmin):
    """Ops visibility into the background AI chart parses -- read-only:
    jobs are created by the dashboard upload flow and mutated only by
    their run_chart_parse worker."""

    list_display = ("__str__", "venue", "status", "progress", "created_at", "finished_at")
    list_filter = ("status", "organization")
    readonly_fields = [f.name for f in ChartParseJob._meta.fields]

    def has_add_permission(self, request):
        return False
