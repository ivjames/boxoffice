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
    # -- team / roles -------------------------------------------------------
    path("dashboard/team/", views.team, name="dashboard_team"),
    path("dashboard/team/add/", views.team_add, name="dashboard_team_add"),
    path(
        "dashboard/team/<int:pk>/role/",
        views.team_update_role,
        name="dashboard_team_update_role",
    ),
    path(
        "dashboard/team/<int:pk>/remove/",
        views.team_remove,
        name="dashboard_team_remove",
    ),
    path("dashboard/orders/", views.OrderListView.as_view(), name="dashboard_order_list"),
    path(
        "dashboard/orders/<slug:token>/",
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
        "dashboard/venues/<int:venue_pk>/charts/parse/",
        views.chart_parse_upload,
        name="dashboard_chart_parse",
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
        "dashboard/charts/<int:chart_pk>/sections/<int:pk>/reorder/",
        views.section_reorder,
        name="dashboard_section_reorder",
    ),
    # -- visual editor (live, param-driven -- docs/EDITOR.md) ---------------
    path(
        "dashboard/charts/<int:pk>/editor/",
        views.chart_editor,
        name="dashboard_chart_editor",
    ),
    path(
        "dashboard/charts/<int:pk>/editor/save/",
        views.chart_editor_save,
        name="dashboard_chart_editor_save",
    ),
    # -- pricing zones (Phase C, docs/SEATING.md) ----------------------------
    path(
        "dashboard/performances/<int:pk>/pricing-zones/",
        views.performance_pricing_zones,
        name="dashboard_performance_pricing_zones",
    ),
    path(
        "dashboard/performances/<int:pk>/pricing-zones/apply/",
        views.performance_zone_apply,
        name="dashboard_performance_zone_apply",
    ),
    path(
        "dashboard/performances/<int:pk>/pricing-zones/remove-seats/",
        views.performance_zone_remove_seats,
        name="dashboard_performance_zone_remove_seats",
    ),
    path(
        "dashboard/performances/<int:pk>/pricing-zones/<int:zone_pk>/delete/",
        views.performance_zone_delete,
        name="dashboard_performance_zone_delete",
    ),
    path(
        "dashboard/performances/<int:pk>/pricing-zones/clone/",
        views.performance_zone_clone,
        name="dashboard_performance_zone_clone",
    ),
    path(
        "dashboard/performances/<int:pk>/pricing-zones/export/",
        views.performance_zone_export,
        name="dashboard_performance_zone_export",
    ),
]
