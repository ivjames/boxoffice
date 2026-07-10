from django.contrib import admin

from .models import GuestAccount


@admin.register(GuestAccount)
class GuestAccountAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "organization", "created_at")
    list_filter = ("organization",)
    search_fields = ("email", "name")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)
