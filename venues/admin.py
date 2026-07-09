from django.contrib import admin

from .models import Seat, SeatingChart, Section, Venue


class SeatInline(admin.TabularInline):
    model = Seat
    extra = 0
    fields = ("row_label", "number", "x", "y", "is_accessible")


class SectionInline(admin.TabularInline):
    model = Section
    extra = 0
    fields = ("name", "ordering")
    show_change_link = True


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "timezone", "address")
    list_filter = ("organization",)
    search_fields = ("name", "address")


@admin.register(SeatingChart)
class SeatingChartAdmin(admin.ModelAdmin):
    list_display = ("name", "venue", "organization")
    list_filter = ("organization", "venue")
    search_fields = ("name", "venue__name")
    inlines = [SectionInline]


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ("name", "chart", "ordering", "organization")
    list_filter = ("organization", "chart")
    search_fields = ("name",)
    inlines = [SeatInline]


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("__str__", "section", "is_accessible", "organization")
    list_filter = ("organization", "is_accessible", "section")
    search_fields = ("row_label", "number")
