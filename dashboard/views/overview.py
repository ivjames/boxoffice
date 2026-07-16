from decimal import Decimal

from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership
from accounts.permissions import tenant_staff_required
from events.models import Event, Performance, PriceTier, PricingZone
from orders.models import Order, Ticket
from orders.services import performance_seats
from venues.models import SeatingChart, Venue


# --- overview / reports ---------------------------------------------------


@tenant_staff_required
def overview(request):
    """Box office and up get the overview. Counts + reports are all scoped
    to request.organization -- see accounts.permissions for how every
    dashboard view gets there (login + Membership-in-this-org check).

    Scanners work the door only: they have no overview at all (the nav hides
    it and login lands them on Scan), so a scanner who reaches this URL
    directly is bounced to the scan screen rather than shown a page their
    role isn't meant to see.

    Revenue is a manager+ concern -- box office sells tickets and services
    the door, it doesn't need the money reports -- so the gross-revenue tile,
    the revenue-by-event table, and the per-performance revenue column are
    only computed (and only rendered, see overview.html) for can_manage_events."""
    if not request.membership.can_sell_tickets():
        return redirect("scan_home")

    organization = request.organization
    now = timezone.now()
    show_revenue = request.membership.can_manage_events()

    # "Getting started" checklist -- manager+ only (same gate as show_revenue
    # above: box office runs the door, it doesn't need setup nagging). Each
    # step is a cheap .exists() query (~10 total), fine for a manager landing
    # page. Auto-hides once every step is done (show_onboarding below) so an
    # established theater never sees it again.
    onboarding_steps = []
    onboarding_done_count = 0
    onboarding_total = 0
    onboarding_all_done = False
    if show_revenue:
        onboarding_steps = [
            {
                "key": "stripe",
                "label": "Connect Stripe payments",
                "done": organization.stripe_charges_enabled,
                # connect_start (payments.views) begins Stripe onboarding, but
                # it's @billing_required + @require_POST -- narrower than this
                # card's manager+ gate. So instead of a plain link (which would
                # 403 a non-billing manager), the step carries a POST action the
                # TEMPLATE renders as a button ONLY for a can_manage_billing
                # user (owners); everyone else sees plain text. The billing role
                # is exactly who can actually finish Stripe, so this is the one
                # actionable path the setup guide points them to.
                "url": None,
                "post_url": reverse("connect_start"),
                "help": "Payouts run through Stripe Connect.",
            },
            {
                "key": "venue",
                "label": "Add a venue",
                "done": Venue.objects.filter(organization=organization).exists(),
                "url": reverse("dashboard_venue_list"),
                "help": "Where your shows happen.",
            },
            {
                "key": "seating_chart",
                "label": "Build a seating chart",
                "done": SeatingChart.objects.filter(organization=organization).exists(),
                "url": reverse("dashboard_venue_list"),
                "help": "Charts live under each venue.",
            },
            {
                "key": "event",
                "label": "Create an event",
                "done": Event.objects.filter(organization=organization).exists(),
                "url": reverse("dashboard_event_list"),
                "help": "",
            },
            {
                "key": "publish_event",
                "label": "Publish an event",
                "done": Event.objects.filter(
                    organization=organization, status=Event.Status.PUBLISHED
                ).exists(),
                "url": reverse("dashboard_event_list"),
                "help": "",
            },
            {
                "key": "price_tier",
                "label": "Set ticket prices",
                # A reserved-seat show can be priced entirely with PricingZones
                # (the pricing resolver checks zones first and a seat needs no
                # PriceTier at all), so a zone-only theater has prices set even
                # with zero PriceTier rows -- count either as evidence, or this
                # step would stay undone and the card never auto-hide for them.
                "done": (
                    PriceTier.objects.filter(organization=organization).exists()
                    or PricingZone.objects.filter(organization=organization).exists()
                ),
                "url": reverse("dashboard_event_list"),
                "help": "",
            },
            {
                "key": "performance_on_sale",
                "label": "Put a performance on sale",
                "done": Performance.objects.filter(
                    organization=organization, status=Performance.Status.PUBLISHED
                ).exists(),
                "url": reverse("dashboard_event_list"),
                "help": "",
            },
            {
                "key": "teammate",
                "label": "Invite a teammate",
                "done": Membership.objects.filter(organization=organization)
                .exclude(role=Membership.Role.OWNER)
                .exists(),
                "url": reverse("dashboard_team"),
                "help": "",
            },
            {
                "key": "branding",
                "label": "Add your logo & colors",
                # Done once a logo is up OR the palette has moved off the
                # ship-time defaults (a preset/custom scheme applied, or colors
                # hand-tweaked) -- either is real branding progress.
                "done": bool(organization.logo)
                or organization.primary_color.lower() != "#111111"
                or organization.accent_color.lower() != "#e11d48",
                "url": reverse("dashboard_branding"),
                "help": "",
            },
            {
                "key": "first_sale",
                "label": "Make your first sale",
                "done": Order.objects.filter(
                    organization=organization, status=Order.Status.PAID
                ).exists(),
                # Informational only -- happens on the storefront, not a
                # dashboard page.
                "url": None,
                "help": "Happens on your storefront once you're set up.",
            },
        ]
        onboarding_total = len(onboarding_steps)
        onboarding_done_count = sum(1 for step in onboarding_steps if step["done"])
        onboarding_all_done = onboarding_done_count == onboarding_total
    show_onboarding = show_revenue and not onboarding_all_done

    upcoming_performances = list(
        Performance.objects.filter(organization=organization, starts_at__gte=now)
        .select_related("event", "venue")
        .order_by("starts_at")[:10]
    )

    tickets_sold = (
        Ticket.objects.filter(organization=organization).exclude(status=Ticket.Status.VOID).count()
    )

    # Revenue from paid orders, aggregated once per grouping rather than
    # per-row in the loops below. Keyed by performance / event id so the
    # per-performance table and the per-event table both read from a dict
    # lookup instead of an N+1 of Sum() queries. Skipped entirely for box
    # office (show_revenue is False) -- no need to query money they can't see.
    gross_revenue = Decimal("0.00")
    revenue_by_performance = {}
    event_revenue_rows = []
    if show_revenue:
        paid_orders = Order.objects.filter(organization=organization, status=Order.Status.PAID)
        # gross_revenue stays computed off EVERY paid order -- donations
        # included -- see this variable's use in overview.html; a donation
        # is real revenue even though it reserves no performance.
        gross_revenue = paid_orders.aggregate(total=Sum("total"))["total"] or Decimal("0.00")

        # The per-performance and per-event groupings below are strictly a
        # TICKETING view (a row per performance / per event), so a Phase 2
        # donation-only order (Order.performance null) must be excluded --
        # left in, it would group under a bogus "performance: None" /
        # "event: None" bucket instead of just not appearing in a table that
        # isn't about it.
        ticketed_orders = paid_orders.filter(performance__isnull=False)

        revenue_by_performance = {
            row["performance"]: row["revenue"]
            for row in ticketed_orders.values("performance").annotate(revenue=Sum("total"))
        }

        event_revenue_rows = [
            {
                "event_title": row["performance__event__title"],
                "orders": row["orders"],
                "revenue": row["revenue"],
            }
            for row in (
                # Group by event id (not title): two distinct events can share a
                # title, and merging them would misreport per-event revenue.
                ticketed_orders.values("performance__event", "performance__event__title")
                .annotate(orders=Count("id"), revenue=Sum("total"))
                .order_by("-revenue")
            )
        ]

    performance_rows = []
    for performance in (
        Performance.objects.filter(organization=organization)
        .select_related("event", "venue")
        .order_by("-starts_at")[:25]
    ):
        sold = (
            Ticket.objects.filter(organization=organization, performance=performance)
            .exclude(status=Ticket.Status.VOID)
            .count()
        )
        if performance.seating_mode == Performance.SeatingMode.GA:
            allocation = getattr(performance, "ga_allocation", None)
            capacity = allocation.capacity if allocation else None
        else:
            capacity = performance_seats(performance).count()
        performance_rows.append(
            {
                "performance": performance,
                "sold": sold,
                "capacity": capacity,
                "revenue": revenue_by_performance.get(performance.id, Decimal("0.00")),
            }
        )

    context = {
        "upcoming_performances": upcoming_performances,
        "tickets_sold": tickets_sold,
        "show_revenue": show_revenue,
        "gross_revenue": gross_revenue,
        "event_revenue_rows": event_revenue_rows,
        "performance_rows": performance_rows,
        "show_onboarding": show_onboarding,
        "onboarding_steps": onboarding_steps,
        "onboarding_done_count": onboarding_done_count,
        "onboarding_total": onboarding_total,
        "onboarding_all_done": onboarding_all_done,
    }
    return render(request, "dashboard/overview.html", context)
