from django.urls import path

from . import views

urlpatterns = [
    # Staff-facing help lives under the dashboard prefix (url names kept
    # dashboard_* so the dashboard nav's active-state logic stays uniform),
    # even though the views live in this app.
    path("dashboard/help/", views.help_index, name="dashboard_help"),
    path("dashboard/help/manage/", views.help_manage, name="dashboard_help_manage"),
    path("dashboard/help/new/", views.help_create, name="dashboard_help_create"),
    path("dashboard/help/<int:pk>/edit/", views.help_update, name="dashboard_help_update"),
    path("dashboard/help/<int:pk>/delete/", views.help_delete, name="dashboard_help_delete"),
    # Storefront (buyer-facing) FAQ.
    path("faq/", views.public_faq, name="faq"),
]
