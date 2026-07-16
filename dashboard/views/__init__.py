"""dashboard.views is a package split by domain -- see each submodule for its
area. This __init__ re-exports every public view/name so `dashboard/urls.py`'s
`from . import views; views.<Name>` keeps working unchanged, and so
`dashboard.views.derive_scheme_from_url` (patched in dashboard/test_branding.py
-- actually dashboard.views.branding.derive_scheme_from_url now, see that
test file) and other cross-module references still resolve where needed."""

from .overview import overview

from .events import (
    EventCreateView,
    EventDetailView,
    EventListView,
    EventUpdateView,
    PerformanceCreateView,
    PerformanceUpdateView,
    performance_detail,
    performance_price_tiers,
)

from .promos import (
    PromoCodeCreateView,
    PromoCodeListView,
    PromoCodeUpdateView,
    promo_deactivate,
)

from .donations import donation_settings, donations_report

from .branding import (
    branding,
    branding_derive,
    branding_harmonize,
    derive_scheme_from_url,
)

from .passes import (
    PassProductCreateView,
    PassProductListView,
    PassProductUpdateView,
    pass_report,
    pass_toggle,
)

from .orders import (
    OrderDetailView,
    OrderListView,
    order_cancel,
    order_refund,
    order_resend,
)

from .venues import (
    SeatingChartCreateView,
    SeatingChartDetailView,
    SeatingChartListView,
    SeatingChartUpdateView,
    SectionCreateView,
    SectionUpdateView,
    VenueListView,
    chart_editor,
    chart_editor_save,
    chart_parse_status,
    chart_parse_upload,
    section_reorder,
)

from .zones import (
    performance_pricing_zones,
    performance_zone_apply,
    performance_zone_clone,
    performance_zone_delete,
    performance_zone_export,
    performance_zone_remove_seats,
)

from .team import team, team_add, team_remove, team_update_role

from .audience import audience_detail, audience_list

from .campaigns import (
    EmailCampaignCreateView,
    EmailCampaignListView,
    EmailCampaignUpdateView,
    campaign_detail,
    campaign_preview,
    campaign_send,
    campaign_test,
)
