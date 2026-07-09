from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/", views.overview, name="dashboard_overview"),
    path("dashboard/events/", views.EventListView.as_view(), name="dashboard_event_list"),
    path("dashboard/events/new/", views.EventCreateView.as_view(), name="dashboard_event_create"),
    path("dashboard/events/<int:pk>/", views.EventDetailView.as_view(), name="dashboard_event_detail"),
    path(
        "dashboard/events/<int:pk>/edit/",
        views.EventUpdateView.as_view(),
        name="dashboard_event_update",
    ),
    path(
        "dashboard/events/<int:event_pk>/performances/new/",
        views.PerformanceCreateView.as_view(),
        name="dashboard_performance_create",
    ),
    path(
        "dashboard/performances/<int:pk>/edit/",
        views.PerformanceUpdateView.as_view(),
        name="dashboard_performance_update",
    ),
    path(
        "dashboard/performances/<int:pk>/price-tiers/",
        views.performance_price_tiers,
        name="dashboard_performance_price_tiers",
    ),
    path("dashboard/orders/", views.OrderListView.as_view(), name="dashboard_order_list"),
    path(
        "dashboard/orders/<uuid:token>/",
        views.OrderDetailView.as_view(),
        name="dashboard_order_detail",
    ),
]
