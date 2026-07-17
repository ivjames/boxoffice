from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/", views.overview, name="dashboard_overview"),
    path(
        "dashboard/performances/<int:pk>/",
        views.performance_detail,
        name="dashboard_performance_detail",
    ),
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
    # -- promo codes (manager+) ----------------------------------------------
    path("dashboard/promos/", views.PromoCodeListView.as_view(), name="dashboard_promo_list"),
    path("dashboard/promos/new/", views.PromoCodeCreateView.as_view(), name="dashboard_promo_create"),
    path(
        "dashboard/promos/<int:pk>/edit/",
        views.PromoCodeUpdateView.as_view(),
        name="dashboard_promo_update",
    ),
    path(
        "dashboard/promos/<int:pk>/toggle/",
        views.promo_deactivate,
        name="dashboard_promo_toggle",
    ),
    # -- donations (manager+) ------------------------------------------------
    path(
        "dashboard/donations/",
        views.donations_report,
        name="dashboard_donation_report",
    ),
    path(
        "dashboard/donations/settings/",
        views.donation_settings,
        name="dashboard_donation_settings",
    ),
    # -- passes (manager+) ----------------------------------------------------
    path("dashboard/passes/", views.PassProductListView.as_view(), name="dashboard_pass_list"),
    path(
        "dashboard/passes/new/", views.PassProductCreateView.as_view(), name="dashboard_pass_create"
    ),
    path(
        "dashboard/passes/<int:pk>/edit/",
        views.PassProductUpdateView.as_view(),
        name="dashboard_pass_update",
    ),
    path(
        "dashboard/passes/<int:pk>/toggle/",
        views.pass_toggle,
        name="dashboard_pass_toggle",
    ),
    path("dashboard/passes/report/", views.pass_report, name="dashboard_pass_report"),
    # -- audience / CRM + email campaigns (manager+, Phase 4) ---------------
    path("dashboard/audience/", views.audience_list, name="dashboard_audience_list"),
    path(
        "dashboard/audience/<int:pk>/",
        views.audience_detail,
        name="dashboard_audience_detail",
    ),
    path(
        "dashboard/campaigns/",
        views.EmailCampaignListView.as_view(),
        name="dashboard_campaign_list",
    ),
    path(
        "dashboard/campaigns/new/",
        views.EmailCampaignCreateView.as_view(),
        name="dashboard_campaign_create",
    ),
    path(
        "dashboard/campaigns/<int:pk>/edit/",
        views.EmailCampaignUpdateView.as_view(),
        name="dashboard_campaign_update",
    ),
    path(
        "dashboard/campaigns/<int:pk>/",
        views.campaign_detail,
        name="dashboard_campaign_detail",
    ),
    path(
        "dashboard/campaigns/<int:pk>/preview/",
        views.campaign_preview,
        name="dashboard_campaign_preview",
    ),
    path(
        "dashboard/campaigns/<int:pk>/test/",
        views.campaign_test,
        name="dashboard_campaign_test",
    ),
    path(
        "dashboard/campaigns/<int:pk>/send/",
        views.campaign_send,
        name="dashboard_campaign_send",
    ),
    # -- branding / color schemes (manager+) --------------------------------
    path("dashboard/branding/", views.branding, name="dashboard_branding"),
    path(
        "dashboard/branding/derive/",
        views.branding_derive,
        name="dashboard_branding_derive",
    ),
    path(
        "dashboard/branding/harmonize/",
        views.branding_harmonize,
        name="dashboard_branding_harmonize",
    ),
    path(
        "dashboard/branding/logo/remove-bg/",
        views.branding_logo_remove_bg,
        name="dashboard_branding_logo_remove_bg",
    ),
    path(
        "dashboard/branding/logo/upload/",
        views.branding_logo_upload,
        name="dashboard_branding_logo_upload",
    ),
    path(
        "dashboard/branding/logo/remove/",
        views.branding_logo_remove,
        name="dashboard_branding_logo_remove",
    ),
    path(
        "dashboard/branding/logo/erase/",
        views.branding_logo_erase,
        name="dashboard_branding_logo_erase",
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
    path(
        "dashboard/orders/<slug:token>/resend/",
        views.order_resend,
        name="dashboard_order_resend",
    ),
    path(
        "dashboard/orders/<slug:token>/cancel/",
        views.order_cancel,
        name="dashboard_order_cancel",
    ),
    path(
        "dashboard/orders/<slug:token>/refund/",
        views.order_refund,
        name="dashboard_order_refund",
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
        "dashboard/parse-jobs/<int:pk>/status/",
        views.chart_parse_status,
        name="dashboard_chart_parse_status",
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
