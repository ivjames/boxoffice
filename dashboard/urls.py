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
    # -- seating chart builder (Phase A, docs/SEATING.md) -------------------
    path("dashboard/venues/", views.VenueListView.as_view(), name="dashboard_venue_list"),
    path(
        "dashboard/venues/<int:venue_pk>/charts/",
        views.SeatingChartListView.as_view(),
        name="dashboard_chart_list",
    ),
    path(
        "dashboard/venues/<int:venue_pk>/charts/new/",
        views.SeatingChartCreateView.as_view(),
        name="dashboard_chart_create",
    ),
    path(
        "dashboard/charts/<int:pk>/",
        views.SeatingChartDetailView.as_view(),
        name="dashboard_chart_detail",
    ),
    path(
        "dashboard/charts/<int:pk>/edit/",
        views.SeatingChartUpdateView.as_view(),
        name="dashboard_chart_update",
    ),
    path(
        "dashboard/charts/<int:chart_pk>/sections/new/",
        views.SectionCreateView.as_view(),
        name="dashboard_section_create",
    ),
    path(
        "dashboard/charts/<int:chart_pk>/sections/<int:pk>/edit/",
        views.SectionUpdateView.as_view(),
        name="dashboard_section_update",
    ),
    path(
        "dashboard/charts/<int:chart_pk>/sections/<int:pk>/",
        views.section_detail,
        name="dashboard_section_detail",
    ),
    path(
        "dashboard/charts/<int:chart_pk>/sections/<int:section_pk>/seats/<int:seat_pk>/toggle-accessible/",
        views.seat_toggle_accessible,
        name="dashboard_seat_toggle_accessible",
    ),
    path(
        "dashboard/charts/<int:chart_pk>/sections/<int:section_pk>/seats/<int:seat_pk>/delete/",
        views.seat_delete,
        name="dashboard_seat_delete",
    ),
]
