from django.contrib import admin

from .models import HelpArticle


@admin.register(HelpArticle)
class HelpArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "organization", "category", "visibility", "is_published", "updated_at")
    list_filter = ("organization", "category", "visibility", "is_published")
    search_fields = ("title", "summary", "body")
    autocomplete_fields = ("organization",)
    readonly_fields = ("created_at", "updated_at")
