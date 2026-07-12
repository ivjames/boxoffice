from django.contrib import admin

from .models import OAuthIdentity


@admin.register(OAuthIdentity)
class OAuthIdentityAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "email", "last_login_at", "created_at")
    list_filter = ("provider",)
    search_fields = ("user__email", "email", "uid")
    readonly_fields = ("created_at", "last_login_at")
    autocomplete_fields = ("user",)
