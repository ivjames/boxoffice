from django.contrib import admin

from .models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "subdomain", "is_active", "currency", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "subdomain", "contact_email")
    prepopulated_fields = {"slug": ("name",)}
