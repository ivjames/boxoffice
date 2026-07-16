import csv
import json
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts import throttle
from accounts.invites import MemberExistsError, add_member
from accounts.models import Membership
from accounts.permissions import (
    BoxOfficeRequiredMixin,
    ManagerRequiredMixin,
    box_office_required,
    manager_required,
    tenant_staff_required,
)
from campaigns.emails import send_test_campaign_email
from campaigns.models import CampaignSend, EmailCampaign
from campaigns.services import CampaignStateError, audience_queryset, segment_recipient_count, start_campaign
from donations.services import get_or_create_general_fund
from events import zones as zone_services
from events.models import Event, Performance, PriceTier, PricingZone, ZoneTemplate
from events.zone_export import ZoneExportError, render_zone_map
from guests.models import GuestAccount
from orders.emails import send_order_receipt
from orders.models import Order, OrderItem, PerformanceSeatBlock, Ticket
from orders.services import get_seating_chart, performance_seats, void_order
from passes.models import PassProduct, PassPurchase
from passes.services import remaining_admissions, restore_redemptions_for_order
from payments.services import RefundError, refund_order
from promotions.models import PromoCode
from tenants.color_extraction import ColorDeriveError, derive_scheme_from_url
from tenants.color_generator import scheme_from_primary
from tenants.color_schemes import COLOR_ROLES, HEX_COLOR_RE
from tenants.fonts import FONTS
from tenants.models import ColorScheme
from venues import chart_parsing, generation
from venues.models import ChartParseJob, Seat, SeatingChart, Section, Venue

from .forms import (
    BrandingForm,
    ColorSchemeForm,
    DonationSettingsForm,
    EmailCampaignForm,
    EventForm,
    GuestTagsNotesForm,
    InviteMemberForm,
    PassProductForm,
    PerformanceForm,
    PriceTierForm,
    PromoCodeForm,
    SeatingChartForm,
    SectionForm,
)


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


@box_office_required
def performance_detail(request, pk):
    """Box-office-facing detail for one performance (linked from the overview's
    upcoming-shows list): a picture of the house (seating chart colored by
    each seat's status for THIS performance), the ticket-sales summary, and a
    guest lookup that finds a specific attendee's ticket by name / email /
    code. Box office+ (managers/owners inherit it); scanners don't reach the
    overview it's linked from, and this view is box_office-gated anyway.

    Revenue stays a manager+ concern here too (mirrors the overview): box
    office gets sold/capacity/checked-in counts, not the money."""
    performance = get_object_or_404(
        Performance.objects.filter(organization=request.organization).select_related(
            "event", "venue", "seating_chart"
        ),
        pk=pk,
    )
    show_revenue = request.membership.can_manage_events()

    tickets_qs = (
        Ticket.objects.filter(organization=request.organization, performance=performance)
        .select_related("seat", "seat__section", "order", "scanned_by")
        .order_by("seat__section__ordering", "seat__row_label", "seat__number", "id")
    )
    live_tickets = list(tickets_qs.exclude(status=Ticket.Status.VOID))
    sold = len(live_tickets)
    checked_in = sum(1 for t in live_tickets if t.status == Ticket.Status.USED)

    if performance.seating_mode == Performance.SeatingMode.GA:
        allocation = getattr(performance, "ga_allocation", None)
        capacity = allocation.capacity if allocation else None
    else:
        capacity = performance_seats(performance).count()

    revenue = None
    if show_revenue:
        revenue = (
            Order.objects.filter(
                organization=request.organization,
                performance=performance,
                status=Order.Status.PAID,
            ).aggregate(total=Sum("total"))["total"]
            or Decimal("0.00")
        )

    # Guest / ticket lookup. Same fields the box office searches on the orders
    # list (buyer name/email/code) plus the per-ticket holder name and code,
    # scoped to THIS performance so a name search returns the seat/status the
    # staffer actually needs at the window or door.
    query = request.GET.get("q", "").strip()
    search_results = None
    if query:
        search_results = list(
            tickets_qs.filter(
                Q(holder_name__icontains=query)
                | Q(order__buyer_name__icontains=query)
                | Q(order__buyer_email__icontains=query)
                | Q(token=query)
                | Q(order__token=query)
            )
        )

    # Seat map. Reserved: a read-only map colored by each seat's status
    # (sold / checked-in / blocked / available), with the holder's name in the
    # tooltip. GA: the same inert "picture of the house" the storefront shows,
    # since GA assigns no seats.
    ga_seats_json = None
    seats_json = None
    if performance.seating_mode == Performance.SeatingMode.GA:
        ga_seats_json = [
            {
                "id": seat.id,
                "row": seat.row_label,
                "number": seat.number,
                "x": seat.x,
                "y": seat.y,
                "section": seat.section.name,
            }
            for seat in performance_seats(performance)
        ]
    else:
        seat_ticket = {t.seat_id: t for t in live_tickets if t.seat_id is not None}
        blocked = set(
            PerformanceSeatBlock.objects.filter(performance=performance).values_list(
                "seat_id", flat=True
            )
        )
        seats_json = []
        for seat in performance_seats(performance):
            ticket = seat_ticket.get(seat.id)
            if ticket is not None:
                state = "used" if ticket.status == Ticket.Status.USED else "sold"
                holder = ticket.holder_name or (
                    ticket.order.buyer_name or ticket.order.buyer_email if ticket.order_id else ""
                )
            elif seat.id in blocked:
                state, holder = "blocked", ""
            else:
                state, holder = "available", ""
            seats_json.append(
                {
                    "id": seat.id,
                    "row": seat.row_label,
                    "number": seat.number,
                    "x": seat.x,
                    "y": seat.y,
                    "section": seat.section.name,
                    "state": state,
                    "holder": holder,
                }
            )

    context = {
        "performance": performance,
        "sold": sold,
        "capacity": capacity,
        "checked_in": checked_in,
        "show_revenue": show_revenue,
        "revenue": revenue,
        "q": query,
        "search_results": search_results,
        "seating_mode": performance.seating_mode,
        "ga_seats_json": ga_seats_json,
        "seats_json": seats_json,
    }
    return render(request, "dashboard/performance_detail.html", context)


# --- events / performances (manager+) -------------------------------------


class EventListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/event_list.html"
    context_object_name = "events"

    def get_queryset(self):
        return Event.objects.filter(organization=self.request.organization).order_by("-created_at")


class EventDetailView(ManagerRequiredMixin, DetailView):
    template_name = "dashboard/event_detail.html"
    context_object_name = "event"

    def get_queryset(self):
        return Event.objects.filter(organization=self.request.organization)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["performances"] = self.object.performances.select_related("venue").order_by("starts_at")
        return context


class EventCreateView(ManagerRequiredMixin, CreateView):
    model = Event
    form_class = EventForm
    template_name = "dashboard/event_form.html"

    def form_valid(self, form):
        form.instance.organization = self.request.organization
        messages.success(self.request, f"Created “{form.instance.title}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_event_detail", args=[self.object.pk])


class EventUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = EventForm
    template_name = "dashboard/event_form.html"

    def get_queryset(self):
        return Event.objects.filter(organization=self.request.organization)

    def form_valid(self, form):
        messages.success(self.request, f"Updated “{form.instance.title}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_event_detail", args=[self.object.pk])


class PerformanceCreateView(ManagerRequiredMixin, CreateView):
    model = Performance
    form_class = PerformanceForm
    template_name = "dashboard/performance_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.event = get_object_or_404(
            Event, pk=kwargs["event_pk"], organization=request.organization
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        kwargs["event"] = self.event
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["event"] = self.event
        return context

    def form_valid(self, form):
        messages.success(self.request, "Performance created.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_event_detail", args=[self.event.pk])


class PerformanceUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = PerformanceForm
    template_name = "dashboard/performance_form.html"

    def get_queryset(self):
        return Performance.objects.filter(organization=self.request.organization)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        kwargs["event"] = self.object.event
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["event"] = self.object.event
        return context

    def form_valid(self, form):
        messages.success(self.request, "Performance updated.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_event_detail", args=[self.object.event_id])


@manager_required
def performance_price_tiers(request, pk):
    performance = get_object_or_404(
        Performance.objects.select_related("event", "venue"),
        pk=pk,
        organization=request.organization,
    )
    if performance.seating_mode == Performance.SeatingMode.RESERVED:
        return _reserved_price_editor(request, performance)

    # GA: a flat performance-scoped tier (or several named types). Unchanged
    # append-only form.
    if request.method == "POST":
        form = PriceTierForm(
            request.POST, organization=request.organization, performance=performance
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Price tier added.")
            return redirect("dashboard_performance_price_tiers", pk=performance.pk)
    else:
        form = PriceTierForm(organization=request.organization, performance=performance)

    return render(
        request,
        "dashboard/performance_price_tiers.html",
        {
            "performance": performance,
            "is_reserved": False,
            "form": form,
            "tiers": performance.price_tiers.all(),
        },
    )


def _reserved_price_editor(request, performance):
    """The reserved-seating pricing editor: one row per Section on the
    performance's chart, showing (and editing inline) that section's
    chart-wide DEFAULT price and, separately, an optional OVERRIDE price for
    THIS performance only. This is the surface a manager sets reserved
    pricing on -- every section is listed whether or not it's been priced
    yet, so an unpriced section is visible instead of silently missing.

    See events.pricing.resolve_seat_tier for the override-then-default rule
    these two columns feed: `PriceTier(performance=None, section=S)` is the
    default, `PriceTier(performance=P, section=S)` the per-performance
    override that wins for this one performance."""
    chart = get_seating_chart(performance)
    sections = list(
        Section.objects.filter(organization=performance.organization_id, chart=chart)
        if chart is not None
        else Section.objects.none()
    )

    if request.method == "POST":
        errors = _save_reserved_prices(request, performance, sections)
        if not errors:
            messages.success(request, "Reserved pricing saved.")
            return redirect("dashboard_performance_price_tiers", pk=performance.pk)
        for message in errors.values():
            messages.error(request, message)
    else:
        errors = {}

    # Current defaults/overrides keyed by section id, so the template can show
    # each section's live values (and echo back a rejected POST unchanged).
    defaults = {
        t.section_id: t
        for t in PriceTier.objects.filter(
            organization=performance.organization_id,
            performance__isnull=True,
            section__in=sections,
        )
    }
    overrides = {
        t.section_id: t
        for t in PriceTier.objects.filter(
            organization=performance.organization_id,
            performance=performance,
            section__in=sections,
        )
    }
    posted = request.POST if request.method == "POST" else None
    rows = []
    for section in sections:
        default_tier = defaults.get(section.id)
        override_tier = overrides.get(section.id)
        if posted is not None:
            default_val = posted.get(f"default_{section.id}", "").strip()
            override_val = posted.get(f"override_{section.id}", "").strip()
        else:
            default_val = "" if default_tier is None else f"{default_tier.amount:.2f}"
            override_val = "" if override_tier is None else f"{override_tier.amount:.2f}"
        rows.append(
            {
                "section": section,
                "default_value": default_val,
                "override_value": override_val,
                "has_default": default_tier is not None,
                "has_override": override_tier is not None,
                "default_error": errors.get(f"default_{section.id}"),
                "override_error": errors.get(f"override_{section.id}"),
            }
        )

    return render(
        request,
        "dashboard/performance_price_tiers.html",
        {
            "performance": performance,
            "is_reserved": True,
            "chart": chart,
            "rows": rows,
        },
    )


def _parse_price(raw):
    """`(amount, error)` for a submitted price cell. Blank is a valid "no
    price" (amount None). A non-numeric or negative value is an error."""
    raw = (raw or "").strip()
    if raw == "":
        return None, None
    try:
        amount = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None, "Enter a number, e.g. 45 or 45.00."
    if amount < 0:
        return None, "Price can't be negative."
    return amount.quantize(Decimal("0.01")), None


def _save_reserved_prices(request, performance, sections):
    """Validate every section's submitted default/override price and, only if
    all are valid, upsert them in one transaction. A blank cell CLEARS that
    section's default (or override): the matching PriceTier is deleted, so a
    manager can un-price a section from the same grid. Returns a
    `{field_name: message}` dict of validation errors ({} on success)."""
    org = request.organization
    errors = {}
    plan = []  # (kind, section, amount-or-None)
    for section in sections:
        default_amount, default_error = _parse_price(request.POST.get(f"default_{section.id}"))
        override_amount, override_error = _parse_price(request.POST.get(f"override_{section.id}"))
        if default_error:
            errors[f"default_{section.id}"] = default_error
        else:
            plan.append(("default", section, default_amount))
        if override_error:
            errors[f"override_{section.id}"] = override_error
        else:
            plan.append(("override", section, override_amount))
    if errors:
        return errors

    with transaction.atomic():
        for kind, section, amount in plan:
            perf = None if kind == "default" else performance
            existing = PriceTier.objects.filter(
                organization=org, performance=perf, section=section
            ).first()
            if amount is None:
                if existing is not None:
                    existing.delete()
                continue
            if existing is not None:
                existing.amount = amount
                existing.save(update_fields=["amount"])
            else:
                name = section.name if kind == "default" else f"{section.name} (this performance)"
                PriceTier.objects.create(
                    organization=org,
                    performance=perf,
                    section=section,
                    name=name,
                    amount=amount,
                )
    return {}


# --- promo codes (manager+) -------------------------------------------------
#
# v1 is org-wide only (no per-event scoping yet -- see promotions.models.
# PromoCode's docstring), so this is a flat list/create/edit CRUD, same shape
# as EventListView/EventCreateView/EventUpdateView above. Codes are never
# hard-deleted (is_active doubles as the archive flag): promo_deactivate is
# the one mutation endpoint, toggling that flag in either direction.


class PromoCodeListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/promo_list.html"
    context_object_name = "promos"

    def get_queryset(self):
        return PromoCode.objects.filter(organization=self.request.organization).order_by(
            "-created_at"
        )


class PromoCodeCreateView(ManagerRequiredMixin, CreateView):
    model = PromoCode
    form_class = PromoCodeForm
    template_name = "dashboard/promo_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        form.instance.organization = self.request.organization
        messages.success(self.request, f"Created promo code {form.instance.code}.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_promo_list")


class PromoCodeUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = PromoCodeForm
    template_name = "dashboard/promo_form.html"

    def get_queryset(self):
        return PromoCode.objects.filter(organization=self.request.organization)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Updated promo code {form.instance.code}.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_promo_list")


@manager_required
@require_POST
def promo_deactivate(request, pk):
    """Toggle a promo code's is_active flag -- doubles as BOTH "deactivate"
    and "reactivate" (the button label flips based on current state; see
    promo_list.html). Codes are never hard-deleted (PromoCode's docstring),
    so this is the only way to retire/restore one. Org-scoped like every
    other dashboard mutation: a pk for another org's code 404s."""
    promo = get_object_or_404(PromoCode, pk=pk, organization=request.organization)
    promo.is_active = not promo.is_active
    promo.save(update_fields=["is_active"])
    if promo.is_active:
        messages.success(request, f"Reactivated {promo.code}.")
    else:
        messages.success(request, f"Deactivated {promo.code}.")
    return redirect("dashboard_promo_list")


# --- donations (manager+) --------------------------------------------------
#
# v1 is a single org-wide campaign (DonationCampaign's docstring), so there's
# one settings form (not a list/create/edit CRUD like promo codes) plus a
# report over paid donation OrderItems -- mirrors the promo section's shape
# where it applies, and the existing dashboard CSV-export convention where
# an endpoint takes `?format=csv` rather than a dedicated URL (see
# donations_report below).


@manager_required
def donation_settings(request):
    """Settings form for the org's single donation campaign -- on/off switch,
    quick-pick preset amounts, and the nonprofit acknowledgment blurb. Loads
    (creating on first visit) via get_or_create_general_fund, same as every
    other donation entry point resolves "the org's campaign"."""
    campaign = get_or_create_general_fund(request.organization)
    if request.method == "POST":
        form = DonationSettingsForm(request.POST, instance=campaign)
        if form.is_valid():
            form.save()
            messages.success(request, "Donation settings saved.")
            return redirect("dashboard_donation_settings")
    else:
        form = DonationSettingsForm(instance=campaign)
    return render(request, "dashboard/donation_settings.html", {"form": form, "campaign": campaign})


# --- branding / color schemes (manager+) -----------------------------------


def _branding_context(request, **extra):
    """Shared context for the branding page: the logo+colors form, the built-in
    presets, and this tenant's own saved schemes. `extra` lets a POST handler
    layer on a derived-palette preview or a bound form with errors."""
    organization = request.organization
    context = {
        "organization": organization,
        "branding_form": BrandingForm(instance=organization),
        # Default "save these colors as a scheme" form, pre-filled with the
        # org's current palette (palette role keys == ColorSchemeForm fields).
        # A POST handler can override with a bound (errored) or derived form.
        "scheme_form": ColorSchemeForm(initial=organization.palette, organization=organization),
        "presets": ColorScheme.objects.filter(is_preset=True),
        "custom_schemes": ColorScheme.objects.filter(organization=organization),
        "roles": COLOR_ROLES,
        "current_palette": organization.palette,
        # key -> CSS stack, so the live-preview JS can resolve a font <select>'s
        # value to an actual font-family without another request.
        "font_stacks": {key: spec["stack"] for key, spec in FONTS.items()},
    }
    context.update(extra)
    return context


def _apply_scheme_to_org(request, scheme):
    organization = request.organization
    organization.apply_color_scheme(scheme)
    messages.success(request, f"Applied “{scheme.name}” to your storefront.")


@manager_required
def branding(request):
    """Logo + six-role brand palette for the storefront. GET shows the current
    colors, the built-in preset gallery, and the tenant's saved custom schemes.
    POST dispatches on an `action` field:

    - save_colors:  save the logo + fonts + hand-picked colors (BrandingForm).
      This is the one editor's primary "Save branding" button.
    - apply_scheme: copy a preset OR one of this tenant's own schemes onto the
      org (scheme lookup is scoped to presets + this org, so a tampered pk
      can't apply another tenant's scheme).
    - save_scheme:  save the editor's six colors as a new named custom scheme
      (the same form's "Save as scheme" button; colors post under the org field
      names, normalized back to role keys here).
    - delete_scheme: delete one of this tenant's own custom schemes.

    Colors are stored on the Organization (the storefront's source of truth);
    schemes are reusable templates, never the live render source.
    """
    organization = request.organization
    action = request.POST.get("action") if request.method == "POST" else None

    if action == "save_colors":
        form = BrandingForm(request.POST, request.FILES, instance=organization)
        if form.is_valid():
            form.save()
            messages.success(request, "Branding saved.")
            return redirect("dashboard_branding")
        return render(request, "dashboard/branding.html", _branding_context(request, branding_form=form))

    if action == "apply_scheme":
        scheme = get_object_or_404(
            ColorScheme.objects.filter(Q(is_preset=True) | Q(organization=organization)),
            pk=request.POST.get("scheme_id"),
        )
        _apply_scheme_to_org(request, scheme)
        return redirect("dashboard_branding")

    if action == "save_scheme":
        # The unified editor posts colors under the Organization field names
        # (primary_color, …); accept the scheme-role names too so the derive
        # flow and a direct role-keyed POST both work. ROLE key wins if present.
        scheme_data = {
            "name": request.POST.get("name", ""),
            **{
                role: request.POST.get(role) or request.POST.get(org_field) or ""
                for role, _label, org_field in COLOR_ROLES
            },
        }
        form = ColorSchemeForm(scheme_data, organization=organization)
        if form.is_valid():
            scheme = form.save()
            messages.success(request, f"Saved “{scheme.name}” to your schemes.")
            if request.POST.get("apply_after_save"):
                _apply_scheme_to_org(request, scheme)
            return redirect("dashboard_branding")
        # Re-render with the name error surfaced and the manager's in-progress
        # logo/font/color edits preserved in the one editor (bound, not saved).
        branding_form = BrandingForm(request.POST, request.FILES, instance=organization)
        return render(
            request,
            "dashboard/branding.html",
            _branding_context(request, branding_form=branding_form, scheme_form=form),
        )

    if action == "delete_scheme":
        scheme = get_object_or_404(
            ColorScheme, pk=request.POST.get("scheme_id"), organization=organization
        )
        name = scheme.name
        scheme.delete()
        messages.success(request, f"Deleted “{name}”.")
        return redirect("dashboard_branding")

    return render(request, "dashboard/branding.html", _branding_context(request))


# Cap on the homepage URL a manager can submit -- generous for real URLs, but
# bounds the input a hostile client can throw at the fetch/guard.
MAX_DERIVE_URL_LEN = 2000


def _derive_error(request, is_ajax, message, status=400, retry_after=None):
    """A derive failure, shaped for the caller: JSON for the inline (fetch)
    flow, a flashed message + redirect for the no-JS fallback. `retry_after`
    (seconds) rides along on rate-limit refusals so the page can count down."""
    if is_ajax:
        payload = {"ok": False, "error": message}
        if retry_after is not None:
            payload["retry_after"] = retry_after
        return JsonResponse(payload, status=status)
    messages.error(request, message)
    return redirect("dashboard_branding")


@manager_required
@require_POST
def branding_derive(request):
    """Run the derive-from-homepage agent (tenants.color_extraction) on a URL
    the manager enters and return the proposed palette.

    Called inline by the branding page's JS (X-Requested-With), it answers with
    JSON the page loads straight into the color pickers + preview -- the manager
    never leaves the page. Without JS it falls back to re-rendering branding.html
    with the palette pre-filled. Either way a fetch/parse failure is a clean
    message, never a 500.

    The endpoint is expensive (external fetch + optional headless render + a
    Claude call), so it's rate-limited per org (throttle.over_limit) and the URL
    length is capped; the agent itself is SSRF-guarded (tenants.color_extraction).
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    org_id = str(request.organization.pk)
    url = (request.POST.get("url") or "").strip()
    if not url:
        return _derive_error(request, is_ajax, "Enter your homepage URL first.")
    if len(url) > MAX_DERIVE_URL_LEN:
        return _derive_error(request, is_ajax, "That web address is too long to read.")

    # Cooldown first (a still-warm org shouldn't spend a window slot just to be
    # refused), then the fixed-window cap.
    cooling = throttle.cooldown_remaining("derive", org_id)
    if cooling > 0:
        return _derive_error(
            request, is_ajax,
            f"Just a moment — you can derive again in {cooling}s.",
            status=429, retry_after=cooling,
        )
    if throttle.over_limit(
        "derive", org_id,
        settings.DERIVE_RATELIMIT_MAX, settings.DERIVE_RATELIMIT_WINDOW_SECONDS,
    ):
        return _derive_error(
            request, is_ajax,
            "You’ve derived a lot of palettes in a short time. Give it a few minutes and try again.",
            status=429,
        )

    try:
        derived = derive_scheme_from_url(url)
    except ColorDeriveError as exc:
        # A failed fetch is cheap and often a fixable typo -- don't start the
        # cooldown, so the manager can correct the URL and retry immediately.
        return _derive_error(request, is_ajax, str(exc))

    # A real derive ran: start the cooldown so the next one can't fire on its heels.
    throttle.start_cooldown("derive", org_id, settings.DERIVE_COOLDOWN_SECONDS)

    if is_ajax:
        return JsonResponse(
            {
                "ok": True,
                "name": derived["name"],
                "roles": derived["roles"],
                "candidates": derived["candidates"],
                "method": derived.get("method", "heuristic"),
                "source_url": derived["source_url"],
                "cooldown": settings.DERIVE_COOLDOWN_SECONDS,
            }
        )

    # No-JS fallback: pre-fill the one editor with the derived colors -- the six
    # pickers (on the Organization field names) and the optional scheme-name
    # input. Saving then runs the ordinary save_colors / save_scheme POST paths.
    organization = request.organization
    scheme_form = ColorSchemeForm(
        initial={"name": derived["name"], **derived["roles"]}, organization=organization
    )
    branding_form = BrandingForm(
        instance=organization,
        initial={
            org_field: derived["roles"][role] for role, _label, org_field in COLOR_ROLES
        },
    )
    return render(
        request,
        "dashboard/branding.html",
        _branding_context(
            request, scheme_form=scheme_form, branding_form=branding_form, derived=derived
        ),
    )


@manager_required
@require_POST
def branding_harmonize(request):
    """Build a full six-role scheme from a single primary color (the branding
    "harmonize" button) using the same rules as the catalog
    (tenants.color_generator.scheme_from_primary), and return it as JSON for the
    page's JS to load into the color pickers + live preview. Nothing is saved --
    it's a client-side suggestion until the manager hits Save."""
    primary = (request.POST.get("primary") or "").strip()
    if not re.match(HEX_COLOR_RE, primary):
        return JsonResponse(
            {"ok": False, "error": "Pick a valid primary color first."}, status=400
        )
    return JsonResponse({"ok": True, "roles": scheme_from_primary(primary)})


@manager_required
def donations_report(request):
    """Totals + a row per paid donation, org-scoped, with optional
    ?start=YYYY-MM-DD&end=YYYY-MM-DD filters on Order.created_at.
    `?format=csv` streams the same rows as a download instead of rendering
    the HTML table -- mirrors the query-param CSV-export convention used
    elsewhere in the dashboard rather than adding a second URL."""
    organization = request.organization
    items = (
        OrderItem.objects.filter(
            organization=organization, kind=OrderItem.Kind.DONATION, order__status=Order.Status.PAID
        )
        .select_related("order", "order__guest", "donation_campaign")
        .order_by("-order__created_at")
    )

    start = request.GET.get("start", "").strip()
    end = request.GET.get("end", "").strip()
    if start:
        items = items.filter(order__created_at__date__gte=start)
    if end:
        items = items.filter(order__created_at__date__lte=end)

    total = items.aggregate(total=Sum("unit_amount"))["total"] or Decimal("0.00")

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="donations.csv"'
        writer = csv.writer(response)
        writer.writerow(["Date", "Order token", "Buyer email", "Buyer name", "Campaign", "Amount"])
        for item in items:
            writer.writerow(
                [
                    item.order.created_at.strftime("%Y-%m-%d %H:%M"),
                    item.order.token,
                    item.order.buyer_email,
                    item.order.buyer_name,
                    item.donation_campaign.name if item.donation_campaign_id else "",
                    item.unit_amount,
                ]
            )
        return response

    return render(
        request,
        "dashboard/donation_report.html",
        {"items": items, "total": total, "start": start, "end": end},
    )


# --- passes (manager+) ------------------------------------------------------
#
# Mirrors the promo-code CRUD shape above: flat list/create/edit, is_active
# doubles as the archive/enable flag (a PassProduct is never hard-deleted --
# its `purchases` PROTECT the row, same stance as PromoCode), pass_toggle is
# the one flip-both-ways mutation endpoint. See passes.models.PassProduct's
# docstring.


class PassProductListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/pass_list.html"
    context_object_name = "products"

    def get_queryset(self):
        return PassProduct.objects.filter(organization=self.request.organization).order_by(
            "-created_at"
        )


class PassProductCreateView(ManagerRequiredMixin, CreateView):
    model = PassProduct
    form_class = PassProductForm
    template_name = "dashboard/pass_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        form.instance.organization = self.request.organization
        messages.success(self.request, f"Created “{form.instance.name}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_pass_list")


class PassProductUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = PassProductForm
    template_name = "dashboard/pass_form.html"

    def get_queryset(self):
        return PassProduct.objects.filter(organization=self.request.organization)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Updated “{form.instance.name}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_pass_list")


@manager_required
@require_POST
def pass_toggle(request, pk):
    """Toggle a pass product's is_active flag -- doubles as BOTH "deactivate"
    (hide from the storefront, block new sales) and "reactivate". Mirrors
    promo_deactivate exactly; past PassPurchases are untouched either way
    (their entitlement terms were already snapshotted at purchase -- see
    PassPurchase's docstring)."""
    product = get_object_or_404(PassProduct, pk=pk, organization=request.organization)
    product.is_active = not product.is_active
    product.save(update_fields=["is_active"])
    if product.is_active:
        messages.success(request, f"Reactivated {product.name}.")
    else:
        messages.success(request, f"Deactivated {product.name}.")
    return redirect("dashboard_pass_list")


@manager_required
def pass_report(request):
    """Sold passes (paid PASS OrderItems), date-filterable + CSV export --
    same shape as donations_report above -- plus OUTSTANDING LIABILITY: how
    many admissions the theater still owes against live (ACTIVE) purchases,
    and roughly what they're worth. A REFUNDED purchase, or a flex purchase
    that's run dry (EXHAUSTED), owes nothing more, so only ACTIVE purchases
    count.

    flex_value_outstanding is computed in PYTHON per purchase (fine at v1
    scale, per the roadmap note) as credits_remaining * (price paid /
    credit_count). "Price paid" is purchase.order.total: fulfill_pass_purchase
    always creates exactly one Order with total=product.price for a pass sale
    (no promo/donation can attach to it), so the order total IS the price paid
    for that pass -- no second query into its OrderItems needed."""
    organization = request.organization
    items = (
        OrderItem.objects.filter(
            organization=organization, kind=OrderItem.Kind.PASS, order__status=Order.Status.PAID
        )
        .select_related("order", "order__guest", "pass_product")
        .order_by("-order__created_at")
    )

    start = request.GET.get("start", "").strip()
    end = request.GET.get("end", "").strip()
    if start:
        items = items.filter(order__created_at__date__gte=start)
    if end:
        items = items.filter(order__created_at__date__lte=end)

    total = items.aggregate(total=Sum("unit_amount"))["total"] or Decimal("0.00")

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="passes.csv"'
        writer = csv.writer(response)
        writer.writerow(["Date", "Order token", "Buyer email", "Product", "Amount"])
        for item in items:
            writer.writerow(
                [
                    item.order.created_at.strftime("%Y-%m-%d %H:%M"),
                    item.order.token,
                    item.order.buyer_email,
                    item.pass_product.name if item.pass_product_id else "",
                    item.unit_amount,
                ]
            )
        return response

    active_purchases = PassPurchase.objects.filter(
        organization=organization, status=PassPurchase.Status.ACTIVE
    ).select_related("order")

    flex_credits_outstanding = 0
    flex_value_outstanding = Decimal("0.00")
    season_admissions_outstanding = 0
    # All-events (empty covered_events) season passes are unbounded -- see
    # passes.services.remaining_admissions's docstring -- so they're counted
    # separately rather than folded into the numeric admissions total.
    unbounded_season_count = 0
    for purchase in active_purchases:
        if purchase.kind == PassProduct.Kind.FLEX:
            remaining = purchase.credits_remaining or 0
            flex_credits_outstanding += remaining
            if purchase.credit_count and remaining:
                price_paid = purchase.order.total if purchase.order_id else Decimal("0.00")
                flex_value_outstanding += (price_paid / purchase.credit_count) * remaining
        else:
            remaining = remaining_admissions(purchase)
            if remaining is None:
                unbounded_season_count += 1
            else:
                season_admissions_outstanding += remaining

    flex_value_outstanding = flex_value_outstanding.quantize(Decimal("0.01"))

    return render(
        request,
        "dashboard/pass_report.html",
        {
            "items": items,
            "total": total,
            "start": start,
            "end": end,
            "flex_credits_outstanding": flex_credits_outstanding,
            "flex_value_outstanding": flex_value_outstanding,
            "season_admissions_outstanding": season_admissions_outstanding,
            "unbounded_season_count": unbounded_season_count,
        },
    )


# --- orders (box_office+) -------------------------------------------------


class OrderListView(BoxOfficeRequiredMixin, ListView):
    template_name = "dashboard/order_list.html"
    context_object_name = "orders"
    paginate_by = 25

    def get_queryset(self):
        qs = Order.objects.filter(organization=self.request.organization).select_related(
            "performance", "performance__event"
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            # token is a short opaque string now (orders.models.new_token), so
            # match it directly instead of parsing the query as a UUID.
            filters = (
                Q(buyer_email__icontains=query)
                | Q(buyer_name__icontains=query)
                | Q(token=query)
            )
            qs = qs.filter(filters)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["q"] = self.request.GET.get("q", "")
        return context


class OrderDetailView(BoxOfficeRequiredMixin, DetailView):
    template_name = "dashboard/order_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        return Order.objects.filter(organization=self.request.organization).select_related(
            "performance", "performance__event", "performance__venue"
        )

    def get_object(self, queryset=None):
        queryset = queryset if queryset is not None else self.get_queryset()
        return get_object_or_404(queryset, token=self.kwargs["token"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tickets"] = self.object.tickets.select_related(
            "seat", "seat__section", "scanned_by"
        ).order_by("id")
        # Phase 2: a kind-aware line-item table -- a ticket item shows its
        # existing seat/tier text, a donation item shows the gift amount +
        # campaign, so a donation-only order's detail page has something to
        # show besides the (now-guarded) performance line and an empty
        # tickets table. select_related covers every FK a line item's kind
        # might read from, so the template never N+1s per row.
        context["items"] = self.object.items.select_related(
            "price_tier", "pricing_zone", "seat", "donation_campaign", "pass_product"
        ).order_by("id")
        # Phase 3: a REDEMPTION order (one that spent a pass on seats) carries
        # PassRedemption rows -- summarize them per pass so the detail page can
        # show "Redeemed with <product>: N ticket(s) (N credit(s))" without a
        # row-per-ticket table. Grouped in Python (not a template {% regroup %}
        # sum) since credits_used needs summing, not just counting.
        redemption_groups = {}
        for redemption in self.object.pass_redemptions.select_related(
            "pass_purchase__product"
        ):
            group = redemption_groups.setdefault(
                redemption.pass_purchase_id,
                {"product": redemption.pass_purchase.product, "count": 0, "credits": 0},
            )
            group["count"] += 1
            group["credits"] += redemption.credits_used
        context["pass_redemption_summary"] = list(redemption_groups.values())
        return context


# --- order actions (box_office+) ------------------------------------------
#
# The staff order surface used to be read-only; these three POST actions are
# what the built-in help center already tells box office they can do (resend
# tickets, cancel/void, refund -- see helpcenter/builtins.py). Each is
# org-scoped by token (a box-office user can't touch another tenant's order)
# and gated to box_office+.


def _org_order(request, token):
    return get_object_or_404(
        Order.objects.filter(organization=request.organization), token=token
    )


@box_office_required
@require_POST
def order_resend(request, token):
    """Re-send the confirmation email for an order (e.g. the buyer lost it or
    gave a typo'd address that's since been corrected) -- tickets, or (Phase
    2) a donation acknowledgment for a donation-only order, via the
    send_order_receipt dispatcher (orders.emails)."""
    order = _org_order(request, token)
    if not order.buyer_email:
        messages.error(request, "This order has no email address on file to send to.")
        return redirect("dashboard_order_detail", token=order.token)
    try:
        send_order_receipt(order)
    except Exception:  # delivery/transport failure -- don't 500 the dashboard
        messages.error(request, "Couldn't send the email just now. Please try again.")
    else:
        messages.success(request, f"Resent the receipt to {order.buyer_email}.")
    return redirect("dashboard_order_detail", token=order.token)


@box_office_required
@require_POST
def order_cancel(request, token):
    """Cancel an order: void its tickets and free the inventory (see
    orders.services.void_order) without moving any money. Use this for a comp/
    test order or when a refund is handled outside the system; use Refund when
    the buyer paid via Stripe and should get their money back."""
    order = _org_order(request, token)
    if order.status in (Order.Status.CANCELLED, Order.Status.REFUNDED):
        messages.info(request, "That order is already cancelled.")
        return redirect("dashboard_order_detail", token=order.token)
    voided = void_order(order)
    # Phase 3: a cancelled PASS-REDEMPTION order comped its tickets against a
    # PassPurchase's entitlement (season event slot / flex credits) -- voiding
    # the tickets alone doesn't give that entitlement back. restore_redemptions_
    # for_order deletes the order's PassRedemption rows (freeing a season event
    # slot) and restores any burned flex credits. A no-op (returns 0) for the
    # common case of an order that never redeemed a pass. dashboard may import
    # passes; orders may not (see passes.services' dependency-direction note),
    # which is why this lives here rather than in orders.services.void_order.
    restore_redemptions_for_order(order)
    order.status = Order.Status.CANCELLED
    order.save(update_fields=["status"])
    messages.success(
        request, f"Cancelled the order and released {voided} ticket(s) back to inventory."
    )
    return redirect("dashboard_order_detail", token=order.token)


@box_office_required
@require_POST
def order_refund(request, token):
    """Refund a paid order in full (Stripe Refund on the connected account for
    a real charge; a recorded reversal for a stub/test order), voiding its
    tickets and freeing inventory -- see payments.services.refund_order.
    Idempotent: refunding an order that isn't currently paid is a no-op."""
    order = _org_order(request, token)
    try:
        refunded = refund_order(order)
    except RefundError:
        messages.error(
            request,
            "Stripe couldn't process the refund. Check the order in the Stripe "
            "dashboard and try again.",
        )
        return redirect("dashboard_order_detail", token=order.token)
    if refunded:
        messages.success(request, "Refunded the order and voided its tickets.")
    else:
        messages.info(request, "That order isn't in a refundable state.")
    return redirect("dashboard_order_detail", token=order.token)


# --- seating chart builder (manager+) --------------------------------------
#
# Phase A of the seating-chart epic (docs/SEATING.md): logical chart CRUD
# (venue -> chart -> section -> generated seats) plus per-seat accessible/
# remove toggles. Every queryset below is scoped to request.organization
# (directly, or transitively through an org-scoped parent already looked up
# with get_object_or_404(..., organization=request.organization)) so a
# manager can never read or act on another org's venue/chart/section/seat --
# see tenants/models.py's "Tenant isolation is non-negotiable" note. The
# visual drag editor and per-performance pricing zones are later phases;
# this is deliberately basic/functional, not the pretty version.


class VenueListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/venue_list.html"
    context_object_name = "venues"

    def get_queryset(self):
        return Venue.objects.filter(organization=self.request.organization).annotate(
            chart_count=Count("seating_charts", distinct=True)
        )


class SeatingChartListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/chart_list.html"
    context_object_name = "charts"

    def dispatch(self, request, *args, **kwargs):
        self.venue = get_object_or_404(
            Venue, pk=kwargs["venue_pk"], organization=request.organization
        )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return SeatingChart.objects.filter(
            organization=self.request.organization, venue=self.venue
        ).annotate(section_count=Count("sections", distinct=True))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["venue"] = self.venue
        # The "Import from image/PDF" panel's job monitor: everything still
        # in flight plus the last few finished runs, so a manager returning
        # to the page sees what happened while they were away.
        context["parse_jobs"] = ChartParseJob.objects.filter(
            organization=self.request.organization, venue=self.venue
        ).select_related("chart")[:5]
        return context


@manager_required
@require_POST
def chart_parse_upload(request, venue_pk):
    """POST target of both "Import from image/PDF" forms (chart list: new
    chart; chart editor sidebar: re-parse INTO the current chart via the
    hidden `chart` field). The parse itself makes two multi-minute vision
    calls -- far past any request timeout -- so this view only validates
    the upload, records a ChartParseJob, and spawns the detached
    run_chart_parse worker (venues.chart_parsing.spawn_parse_job); progress
    is polled from chart_parse_status. Manager-gated and venue-scoped like
    every other chart mutation. AJAX callers (the editor panel) get JSON
    {ok, job_id, status_url}; plain form POSTs bounce back to the chart
    list, where the jobs panel picks the new job up."""
    venue = get_object_or_404(Venue, pk=venue_pk, organization=request.organization)
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    back = redirect("dashboard_chart_list", venue.pk)

    def fail(message, status=400):
        if wants_json:
            return JsonResponse({"ok": False, "error": message}, status=status)
        messages.error(request, message)
        return back

    upload = request.FILES.get("file")
    if upload is None:
        return fail("Choose an image or PDF of your seating chart first.")
    media_type = chart_parsing.media_type_for_upload(upload.name, upload.content_type)
    if media_type is None:
        return fail("Unsupported file type. Upload a PNG, JPEG, GIF, WebP image or a PDF.")
    if upload.size > chart_parsing.MAX_UPLOAD_BYTES:
        return fail("File is too large (20 MB max).")

    replace_chart = None
    replace_pk = request.POST.get("chart")
    if replace_pk:
        replace_chart = get_object_or_404(
            SeatingChart, pk=replace_pk, organization=request.organization, venue=venue
        )

    job = ChartParseJob.objects.create(
        organization=request.organization,
        venue=venue,
        replace_chart=replace_chart,
        upload=upload,
        media_type=media_type,
        chart_name=(request.POST.get("name") or "").strip(),
        created_by=request.user,
    )
    chart_parsing.spawn_parse_job(job)

    status_url = reverse("dashboard_chart_parse_status", args=[job.pk])
    if wants_json:
        return JsonResponse({"ok": True, "job_id": job.pk, "status_url": status_url})
    messages.info(
        request,
        f"Parsing {upload.name} in the background -- it usually takes a couple of minutes. "
        "Progress shows below; you can leave this page and come back.",
    )
    return back


@manager_required
def chart_parse_status(request, pk):
    """Polling endpoint for a ChartParseJob (the chart list panel and the
    editor sidebar poll it every few seconds). Org-scoped like everything
    else; reports effective_status so a dead worker reads as failed rather
    than spinning forever."""
    job = get_object_or_404(ChartParseJob, pk=pk, organization=request.organization)
    status = job.effective_status
    payload = {
        "status": status,
        "progress": job.progress or None,
        "error": job.error or None,
        "usage": chart_parsing.describe_usage(job.usage) or None,
        "editor_url": None,
        "detail": None,
    }
    if status == ChartParseJob.Status.FAILED and not payload["error"]:
        payload["error"] = "The parse worker stopped responding. Try again."
    if status == ChartParseJob.Status.SUCCEEDED and job.chart_id:
        payload["editor_url"] = reverse("dashboard_chart_editor", args=[job.chart_id])
        seat_count = Seat.objects.filter(
            organization=request.organization, section__chart_id=job.chart_id
        ).count()
        payload["detail"] = (
            f"{job.chart.sections.count()} section(s) / {seat_count} seat(s)"
        )
    return JsonResponse(payload)


class SeatingChartCreateView(ManagerRequiredMixin, CreateView):
    model = SeatingChart
    form_class = SeatingChartForm
    template_name = "dashboard/chart_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.venue = get_object_or_404(
            Venue, pk=kwargs["venue_pk"], organization=request.organization
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        kwargs["venue"] = self.venue
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["venue"] = self.venue
        return context

    def form_valid(self, form):
        messages.success(self.request, "Seating chart created.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_chart_detail", args=[self.object.pk])


class SeatingChartUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = SeatingChartForm
    template_name = "dashboard/chart_form.html"

    def get_queryset(self):
        return SeatingChart.objects.filter(organization=self.request.organization)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        kwargs["venue"] = self.object.venue
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["venue"] = self.object.venue
        return context

    def form_valid(self, form):
        messages.success(self.request, "Seating chart updated.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_chart_detail", args=[self.object.pk])


class SeatingChartDetailView(ManagerRequiredMixin, DetailView):
    template_name = "dashboard/chart_detail.html"
    context_object_name = "chart"

    def get_queryset(self):
        return SeatingChart.objects.filter(organization=self.request.organization).select_related(
            "venue"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["sections"] = (
            self.object.sections.annotate(seat_count=Count("seats"))
            .order_by("ordering", "name")
        )
        return context


class SectionCreateView(ManagerRequiredMixin, CreateView):
    """Section-shell creation (name/tier/numbering -- layout params are the
    live editor's job, see SectionForm's docstring). Round 2's "add sections
    without leaving the editor" (docs/EDITOR.md) reuses this SAME manager-
    gated, org-/chart-scoped endpoint for BOTH paths rather than adding a
    parallel one:

    - A plain (non-AJAX) form POST -- e.g. someone hits /sections/new/
      directly -- behaves exactly as before: redirects into the editor with
      the new section selected (?section=<pk>), which the editor's own
      init() already reads as `initialSelectedId`.
    - An AJAX POST (chart_editor.js's inline "New section" modal sends
      `X-Requested-With: XMLHttpRequest`) gets a JSON response instead --
      `{"ok": true, "section": {...}}` in the exact shape _section_json
      produces, so the editor can splice it straight into its live `sections`
      state with no extra fetch/reload -- and `{"ok": false, "errors":
      {...}}` on validation failure (e.g. a duplicate section name), instead
      of a re-rendered HTML form page an XHR caller can't use.
    """

    model = Section
    form_class = SectionForm
    template_name = "dashboard/section_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.chart = get_object_or_404(
            SeatingChart, pk=kwargs["chart_pk"], organization=request.organization
        )
        return super().dispatch(request, *args, **kwargs)

    def _wants_json(self):
        return self.request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        kwargs["chart"] = self.chart
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["chart"] = self.chart
        return context

    def form_valid(self, form):
        # A fresh section's layout params are all still model defaults
        # (origin 0,0) -- stagger new sections' origin_x a bit so they
        # don't all land exactly on top of each other in the live editor;
        # staff drag the section (or its rotation pivot) to place it
        # precisely afterward. `ordering` isn't a form field (see
        # SectionForm's docstring) -- append this section to the end of the
        # chart's current list the same way; reordering afterward is the
        # sidebar's up/down arrows (section_reorder below), not a manual
        # number.
        existing_count = Section.objects.filter(organization=self.request.organization, chart=self.chart).count()
        form.instance.origin_x = existing_count * 12.0
        form.instance.ordering = existing_count
        response = super().form_valid(form)  # sets self.object; builds the redirect response
        if self._wants_json():
            color = _section_color(existing_count)
            return JsonResponse({"ok": True, "section": _section_json(self.object, color, self.chart.pk)})
        messages.success(self.request, "Section created -- shape and place it in the visual editor.")
        return response

    def form_invalid(self, form):
        if self._wants_json():
            return JsonResponse(
                {"ok": False, "errors": {field: list(errs) for field, errs in form.errors.items()}},
                status=400,
            )
        return super().form_invalid(form)

    def get_success_url(self):
        return f"{reverse('dashboard_chart_editor', args=[self.chart.pk])}?section={self.object.pk}"


class SectionUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = SectionForm
    template_name = "dashboard/section_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.chart = get_object_or_404(
            SeatingChart, pk=kwargs["chart_pk"], organization=request.organization
        )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return Section.objects.filter(organization=self.request.organization, chart=self.chart)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        kwargs["chart"] = self.chart
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["chart"] = self.chart
        return context

    def form_valid(self, form):
        messages.success(self.request, "Section updated.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_chart_detail", args=[self.chart.pk])


@manager_required
@require_POST
def section_reorder(request, chart_pk, pk):
    """Move one section up or down in its chart's display order -- Round
    2's reordering mechanism now that `ordering` isn't a manual number field
    on SectionForm (see its docstring): a small swap-with-neighbor action,
    exposed as up/down arrows in the chart editor sidebar, instead of asking
    staff to think in raw sort integers. JSON body: `{"direction": "up" |
    "down"}`. Swaps THIS section's `ordering` value with its neighbor's in
    the chart's current `(ordering, name)` order -- a no-op (still 200) if
    already first/last. Org- AND chart-scoped like every other section
    mutation; returns the chart's full section id order afterward so the
    editor can just replace its local `sectionOrder` array."""
    chart = get_object_or_404(SeatingChart, pk=chart_pk, organization=request.organization)
    section = get_object_or_404(Section, pk=pk, organization=request.organization, chart=chart)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = {}
    direction = payload.get("direction")
    if direction not in ("up", "down"):
        return JsonResponse({"ok": False, "error": "direction must be 'up' or 'down'."}, status=400)

    ordered = list(chart.sections.order_by("ordering", "name"))
    index = next((i for i, s in enumerate(ordered) if s.pk == section.pk), None)
    neighbor_index = index - 1 if direction == "up" else index + 1

    if index is not None and 0 <= neighbor_index < len(ordered):
        neighbor = ordered[neighbor_index]
        with transaction.atomic():
            section.ordering, neighbor.ordering = neighbor.ordering, section.ordering
            section.save(update_fields=["ordering"])
            neighbor.save(update_fields=["ordering"])

    new_order = [s.pk for s in chart.sections.order_by("ordering", "name")]
    return JsonResponse({"ok": True, "order": new_order})


# --- seating chart visual editor (live, param-driven -- docs/EDITOR.md) ---
#
# Rework of the Phase B SVG drag editor: the canvas is entirely param-driven
# and live -- static/js/seat_geometry.js mirrors venues.generation's
# formulas so the browser computes/redraws a section's seats with zero
# server round-trips as sliders/handles move (no per-seat dragging, no
# "Regenerate" button/flow anywhere -- see chart_editor.js). This view only
# ships each section's CURRENT params (+ its removed/accessible seat-
# identity overrides) as JSON; chart_editor_save below is the only thing
# that persists them, and it's also the only place seats actually get
# (re)generated server-side (venues.generation.generate_seats, same
# formulas, authoritative). Phase C's drag-select pricing zones reuse the
# same SVG seat-element structure (`<circle class="editor-seat"
# data-seat-id data-section-id ...>`, rendered by
# performance_pricing_zones/zone_editor.html against real, already-
# persisted Seat rows) -- unaffected by this rework.

_SECTION_PALETTE = [
    "#e11d48", "#2563eb", "#059669", "#d97706", "#7c3aed",
    "#0891b2", "#db2777", "#65a30d", "#4f46e5", "#dc2626",
]


def _section_color(index):
    return _SECTION_PALETTE[index % len(_SECTION_PALETTE)]


# Every Section field the live editor reads/writes -- the single list both
# chart_editor (serializing for the page) and chart_editor_save
# (deserializing + persisting) iterate, so the two can never drift apart on
# which fields are in scope. pivot_mode/pivot_x/pivot_y are Round 2's
# configurable-rotation-pivot fields (docs/EDITOR.md) -- see
# venues.generation's module docstring and Section.pivot_mode's help text.
_SECTION_PARAM_FIELDS = [
    "origin_x", "origin_y", "rotation", "seat_pitch", "row_pitch", "row_x_offset",
    "arc_radius", "offset_mode", "alt_row_seat_delta", "rows", "seats_per_row",
    "numbering_scheme", "seat_number_base", "row_label_scheme", "row_label_start",
    "pivot_mode", "pivot_x", "pivot_y",
]


def _section_json(section, color, chart_pk):
    """The JSON shape the live editor's Alpine state needs for one section
    -- shared by chart_editor's initial-page json_script payload AND
    SectionCreateView's inline-add AJAX response (docs/EDITOR.md Round 2's
    "add sections without leaving the editor"), so a section created inline
    is indistinguishable, client-side, from one the page loaded with."""
    return {
        "id": section.pk,
        "name": section.name,
        "tier": section.tier,
        "color": color,
        "edit_url": reverse("dashboard_section_update", args=[chart_pk, section.pk]),
        "reorder_url": reverse("dashboard_section_reorder", args=[chart_pk, section.pk]),
        **{field: getattr(section, field) for field in _SECTION_PARAM_FIELDS},
        "removed_seats": section.removed_seats,
        "accessible_seats": section.accessible_seats,
    }


@manager_required
def chart_editor(request, pk):
    """GET-only: renders the chart editor shell. Every section's current
    params (+ its removed_seats/accessible_seats overrides) are embedded via
    json_script (same pattern as templates/orders/_seat_map.html) so
    chart_editor.js can compute and draw the whole live seat map --
    including the very first paint -- entirely client-side via
    seat_geometry.js, with no seats/view_box computation needed here."""
    chart = get_object_or_404(
        SeatingChart.objects.select_related("venue"), pk=pk, organization=request.organization
    )
    sections = list(chart.sections.order_by("ordering", "name"))

    sections_json = [
        _section_json(section, _section_color(i), chart.pk) for i, section in enumerate(sections)
    ]

    selected_param = request.GET.get("section")
    try:
        selected_id = int(selected_param) if selected_param else None
    except ValueError:
        selected_id = None
    if selected_id is not None and not any(s.pk == selected_id for s in sections):
        selected_id = None

    return render(
        request,
        "dashboard/chart_editor.html",
        {
            "chart": chart,
            "sections": sections,
            "sections_json": sections_json,
            "initial_selected_id": selected_id,
            "save_url": reverse("dashboard_chart_editor_save", args=[chart.pk]),
            "new_section_url": reverse("dashboard_section_create", args=[chart.pk]),
        },
    )


def _clean_identity_pairs(raw):
    """`raw` (whatever the client sent for removed/accessible) down to a
    set of (row_label, number) string tuples -- silently drops anything
    that isn't a 2-element [str, str]-ish pair rather than 400ing the whole
    save over one malformed entry."""
    pairs = set()
    if not isinstance(raw, list):
        return pairs
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            row, number = item
            if isinstance(row, str) and row:
                pairs.add((row, str(number)))
    return pairs


@manager_required
@require_POST
def chart_editor_save(request, pk):
    """Batch-persist every section's live-edited params in one request --
    JSON body `{"sections": {section_id: {<params...>, "removed": [[row,
    number], ...], "accessible": [[row, number], ...]}, ...}}` (the exact
    shape chart_editor.js's buildPayload() produces). Org- AND chart-
    scoped: a section id that doesn't belong to THIS organization's THIS
    chart is silently skipped -- never mutated, never even distinguished
    from "doesn't exist" in the response (same tenant-isolation shape the
    old positions-save endpoint used).

    Each section is regenerated via venues.generation.generate_seats
    (replace=True) with its new params, applying the removed/accessible
    identity overrides -- the SAME formulas the client's live canvas just
    used, so what staff see is exactly what gets persisted. Keeps the
    Phase-A guardrail: generate_seats refuses outright (per-section, not
    for the whole batch) if any of that section's existing seats has a
    live (non-void) ticket -- that section's params are left untouched
    (nothing about it is saved) and its error comes back in `errors`,
    while every other valid section in the same request still saves."""
    chart = get_object_or_404(SeatingChart, pk=pk, organization=request.organization)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    sections_payload = payload.get("sections")
    if not isinstance(sections_payload, dict) or not sections_payload:
        return JsonResponse({"ok": False, "error": "No sections given."}, status=400)

    try:
        section_ids = [int(section_id) for section_id in sections_payload.keys()]
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid section id."}, status=400)

    # Tenant-isolation gate: filtered by BOTH organization and this chart,
    # so a section id for another org (or another chart in this org) just
    # isn't in `sections` below -- silently excluded, never touched.
    sections = {
        section.pk: section
        for section in Section.objects.filter(
            pk__in=section_ids, organization=request.organization, chart=chart
        )
    }

    saved = []
    errors = {}
    for section_id, raw in sections_payload.items():
        section = sections.get(int(section_id))
        if section is None or not isinstance(raw, dict):
            continue

        try:
            for field in _SECTION_PARAM_FIELDS:
                if field not in raw:
                    continue
                if field in ("offset_mode", "numbering_scheme", "row_label_scheme", "pivot_mode"):
                    setattr(section, field, str(raw[field]))
                elif field == "arc_radius":
                    setattr(section, field, None if raw[field] in (None, "", 0) else float(raw[field]))
                elif field in ("alt_row_seat_delta",):
                    # Round 3 (docs/EDITOR.md #9): alt-row add/drop is a
                    # small brick-stagger nudge, not a general seat-count
                    # control -- clamp server-side to -1/0/+1 regardless of
                    # what the client sent (the editor's own stepper already
                    # clamps the same way, see chart_editor.js's
                    # stepAltDelta -- this is the authoritative backstop).
                    setattr(section, field, max(-1, min(1, int(raw[field]))))
                elif field == "row_x_offset":
                    # Round-4 correction (docs/EDITOR.md): round 3 raised
                    # the offset range too far -- the user actually wants
                    # it capped at +/-2, same as the editor's now-fixed
                    # slider (chart_editor.js's offsetRange()). Clamp here
                    # too, the authoritative backstop against a stale/
                    # tampered client value (same pattern as
                    # alt_row_seat_delta above).
                    setattr(section, field, max(-2.0, min(2.0, float(raw[field]))))
                elif field in ("rows", "seats_per_row"):
                    setattr(section, field, max(1, int(raw[field])))
                elif field in ("row_label_start", "seat_number_base"):
                    setattr(section, field, max(0, int(raw[field])))
                else:
                    setattr(section, field, float(raw[field]))
        except (TypeError, ValueError):
            errors[section_id] = "Invalid layout parameter value."
            continue

        removed_ids = _clean_identity_pairs(raw.get("removed"))
        accessible_ids = _clean_identity_pairs(raw.get("accessible"))
        row_counts = generation.compute_row_counts(
            section.rows, section.seats_per_row, section.offset_mode, section.alt_row_seat_delta
        )

        try:
            with transaction.atomic():
                try:
                    # Preferred path: the seat roster is unchanged (a pure
                    # move/rotate/re-pitch/arc/accessible-toggle edit), so
                    # update the existing seats' coordinates in place --
                    # preserving their pks and therefore any tickets/holds
                    # attached to them. This is why moving a section with
                    # live tickets no longer trips the orphan guardrail.
                    generation.reposition_seats(
                        section,
                        row_counts,
                        removed_ids=removed_ids,
                        accessible_ids=accessible_ids,
                    )
                except generation.SeatRosterChanged:
                    # The edit actually adds/removes seats -- there's no 1:1
                    # mapping onto the existing rows, so fall back to a full
                    # regenerate, which enforces the live-ticket guardrail
                    # (deleting a seat under an issued ticket is never safe).
                    generation.generate_seats(
                        section,
                        row_counts,
                        removed_ids=removed_ids,
                        accessible_ids=accessible_ids,
                        replace=True,
                    )
                section.removed_seats = sorted(removed_ids)
                section.accessible_seats = sorted(accessible_ids)
                section.save(update_fields=_SECTION_PARAM_FIELDS + ["removed_seats", "accessible_seats"])
        except generation.SeatGenerationError as exc:
            errors[section_id] = str(exc)
            continue

        saved.append(section.pk)

    return JsonResponse({"ok": not errors, "saved": saved, "errors": errors})


# --- pricing zones (Phase C, docs/SEATING.md) ------------------------------
#
# Per-PERFORMANCE visual pricing-zone editor: marquee (rubber-band) + shift-
# click selection over the same SVG seat elements Phase B's chart editor
# renders (every seat is `<circle class="editor-seat" data-seat-id
# data-section-id>` -- see zone_editor.html/static/js/zone_editor.js), then
# "apply a zone" to the selection (pick an existing ZoneTemplate or define a
# new name+color on the fly -- see events.zones.get_or_create_template),
# remove seats from a zone, delete a zone, or clone another performance's
# zones wholesale. Every endpoint below is manager-gated (ManagerRequired
# via manager_required) and scoped to request.organization + this one
# performance, same tenant-isolation shape as the Phase B save endpoint.


def _zones_payload(performance):
    """[{id, name, color, amount, template_id, seat_ids}] for every
    PricingZone currently on `performance` -- the shape both the initial
    page render (json_script) and every mutation endpoint's JSON response
    share, so the client-side Alpine component can just replace its local
    zones array after any successful mutation instead of re-fetching."""
    zones = (
        PricingZone.objects.filter(organization=performance.organization_id, performance=performance)
        .prefetch_related("seats")
        .order_by("name")
    )
    return [
        {
            "id": zone.pk,
            "name": zone.name,
            "color": zone.color,
            "amount": str(zone.amount),
            "template_id": zone.template_id,
            "seat_ids": [seat.pk for seat in zone.seats.all()],
        }
        for zone in zones
    ]


def _get_org_scoped_performance(request, pk):
    """The Performance for this pk, scoped to request.organization (404
    otherwise -- never leaks a cross-org performance's existence). Callers
    still need to check seating_mode themselves where a redirect (not a 404)
    is the right response for a GA performance (see performance_pricing_zones
    below) vs. a plain 400 for the JSON mutation endpoints."""
    return get_object_or_404(
        Performance.objects.select_related("event", "venue"),
        pk=pk,
        organization=request.organization,
    )


@manager_required
def performance_pricing_zones(request, pk):
    """GET-only: renders the performance's chart as one inline SVG (same
    section-color/seat-radius/view_box computation as chart_editor above,
    reused for continuity between the two editors) plus the zone/template
    data the Alpine component needs -- marquee/shift-click selection state
    and all mutation calls are client-side (static/js/zone_editor.js)."""
    performance = _get_org_scoped_performance(request, pk)
    if performance.seating_mode != Performance.SeatingMode.RESERVED:
        messages.error(request, "Pricing zones are only for reserved-seating performances.")
        return redirect("dashboard_event_detail", pk=performance.event_id)

    # Shared with events.zone_export (Phase D's PNG/PDF export) so the two
    # can never visually drift -- see zone_services.zone_map_geometry.
    sections, seats, seat_radius, (view_min_x, view_min_y, view_w, view_h) = (
        zone_services.zone_map_geometry(performance)
    )

    section_color_by_id = {section.pk: _section_color(i) for i, section in enumerate(sections)}
    for seat in seats:
        seat.editor_color = section_color_by_id.get(seat.section_id, "#6b7280")

    view_box = f"{view_min_x} {view_min_y} {view_w} {view_h}"

    templates = ZoneTemplate.objects.filter(organization=request.organization).order_by("name")
    templates_json = [{"id": t.pk, "name": t.name, "color": t.color} for t in templates]

    other_performances = (
        Performance.objects.filter(
            organization=request.organization, seating_mode=Performance.SeatingMode.RESERVED
        )
        .exclude(pk=performance.pk)
        .select_related("event")
        .order_by("-starts_at")
    )

    return render(
        request,
        "dashboard/zone_editor.html",
        {
            "performance": performance,
            "seats": seats,
            "zones_json": _zones_payload(performance),
            "templates_json": templates_json,
            "other_performances": other_performances,
            "view_box": view_box,
            "seat_radius": seat_radius,
            "apply_url": reverse("dashboard_performance_zone_apply", args=[performance.pk]),
            "remove_url": reverse("dashboard_performance_zone_remove_seats", args=[performance.pk]),
            # zone_editor.js builds each zone's own delete URL from this
            # (reverse() needs a concrete zone pk, but there's one delete URL
            # PER zone, not one per page) by appending `<zone_id>/delete/`.
            "delete_url_prefix": reverse("dashboard_performance_zone_delete", args=[performance.pk, 1])[
                : -len("1/delete/")
            ],
            "clone_url": reverse("dashboard_performance_zone_clone", args=[performance.pk]),
            "export_url": reverse("dashboard_performance_zone_export", args=[performance.pk]),
        },
    )


def _json_body(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _clean_seat_ids(request, performance, raw):
    """Validate `raw` (whatever the client sent as seat_ids) down to the
    subset that are real ints AND actually belong to THIS org's THIS
    performance's chart -- exactly the tenant-isolation shape
    chart_editor_save uses, so a seat id for another org/chart/performance
    is silently dropped, never mutated."""
    if not isinstance(raw, list):
        return []
    try:
        seat_ids = [int(s) for s in raw]
    except (TypeError, ValueError):
        return []
    chart = get_seating_chart(performance)
    if chart is None:
        return []
    return list(
        Seat.objects.filter(
            pk__in=seat_ids, organization=request.organization, section__chart=chart
        ).values_list("pk", flat=True)
    )


@manager_required
@require_POST
def performance_zone_apply(request, pk):
    """Batch-assign the selection to a zone. JSON body:
    `{"seat_ids": [...], "amount": "45.00", "template_id": 5}` to reuse an
    existing ZoneTemplate, OR `{"seat_ids": [...], "amount": "45.00",
    "name": "Premium", "color": "#c1121f"}` to define one on the fly (turned
    into a real, reusable ZoneTemplate via get_or_create_template -- see its
    docstring). Returns the full current zone list so the client can just
    replace its local state."""
    performance = _get_org_scoped_performance(request, pk)
    if performance.seating_mode != Performance.SeatingMode.RESERVED:
        return JsonResponse({"ok": False, "error": "Not a reserved-seating performance."}, status=400)

    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    seat_ids = _clean_seat_ids(request, performance, payload.get("seat_ids"))
    if not seat_ids:
        return JsonResponse({"ok": False, "error": "No valid seats selected."}, status=400)

    try:
        amount = Decimal(str(payload.get("amount", "")).strip())
    except InvalidOperation:
        return JsonResponse({"ok": False, "error": "Enter a valid price."}, status=400)
    if amount < 0:
        return JsonResponse({"ok": False, "error": "Price can't be negative."}, status=400)

    template_id = payload.get("template_id")
    if template_id:
        template = get_object_or_404(ZoneTemplate, pk=template_id, organization=request.organization)
    else:
        name = (payload.get("name") or "").strip()
        color = (payload.get("color") or "").strip()
        if not name or not color:
            return JsonResponse(
                {"ok": False, "error": "Pick an existing zone or give the new one a name and color."},
                status=400,
            )
        template = zone_services.get_or_create_template(
            organization=request.organization, name=name, color=color
        )

    try:
        zone_services.apply_zone(
            organization=request.organization,
            performance=performance,
            seat_ids=seat_ids,
            amount=amount,
            template=template,
        )
    except zone_services.ZoneError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


@manager_required
@require_POST
def performance_zone_remove_seats(request, pk):
    """Unassign seats from a zone. JSON body: `{"zone_id": 3, "seat_ids":
    [...]}`. The zone itself is looked up scoped to org + this performance,
    so a zone id for another performance/org 404s instead of silently doing
    nothing (an explicit staff action deserves a clear error, unlike the
    seat-id list which just gets filtered)."""
    performance = _get_org_scoped_performance(request, pk)
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    zone = get_object_or_404(
        PricingZone, pk=payload.get("zone_id"), organization=request.organization, performance=performance
    )
    seat_ids = _clean_seat_ids(request, performance, payload.get("seat_ids"))
    if not seat_ids:
        return JsonResponse({"ok": False, "error": "No valid seats given."}, status=400)

    zone_services.remove_seats_from_zone(organization=request.organization, zone=zone, seat_ids=seat_ids)
    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


@manager_required
@require_POST
def performance_zone_delete(request, pk, zone_pk):
    """Delete a zone outright (its seats fall back to their section's
    PriceTier -- events.pricing.resolve_seat_price). Any hold/order that
    already snapshotted this zone's price is unaffected -- see HoldSeat's
    docstring."""
    performance = _get_org_scoped_performance(request, pk)
    zone = get_object_or_404(
        PricingZone, pk=zone_pk, organization=request.organization, performance=performance
    )
    zone_services.delete_zone(organization=request.organization, zone=zone)
    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


@manager_required
@require_POST
def performance_zone_clone(request, pk):
    """Clone another performance's zones (incl. seat-sets + prices) onto
    this one as brand-new PricingZone instances -- JSON body:
    `{"source_performance_id": 7}`. The source performance is looked up
    scoped to THIS org only (never another tenant's), but does NOT have to
    share this performance's chart -- events.zones.clone_zones_from_performance
    only copies the seats that are actually part of both."""
    performance = _get_org_scoped_performance(request, pk)
    if performance.seating_mode != Performance.SeatingMode.RESERVED:
        return JsonResponse({"ok": False, "error": "Not a reserved-seating performance."}, status=400)

    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    source_performance = get_object_or_404(
        Performance, pk=payload.get("source_performance_id"), organization=request.organization
    )
    zone_services.clone_zones_from_performance(
        organization=request.organization,
        target_performance=performance,
        source_performance=source_performance,
    )
    return JsonResponse({"ok": True, "zones": _zones_payload(performance)})


_EXPORT_CONTENT_TYPES = {"png": "image/png", "pdf": "application/pdf"}


@manager_required
def performance_zone_export(request, pk):
    """GET-only: renders `performance`'s pricing-zone map to PNG or PDF
    (Phase D, docs/SEATING.md "D") and returns it as a download. Org-scoped
    via _get_org_scoped_performance -- a cross-org performance pk 404s
    before any rendering happens, same as every other endpoint on this
    page. Query params (all optional, matching the zone-editor export
    form's field names):

    - `format`: "png" (default) or "pdf".
    - `size`: "letter" (default) or "legal".
    - `labels`: "0" to omit seat row/number labels (default on).
    - `legend`: "0" to omit the zone/price legend (default on).

    Unlike the JSON mutation endpoints above, this isn't restricted to
    RESERVED performances at the HTTP layer -- events.zone_export.render_zone_map
    just renders whatever seats/zones exist (none, for a GA performance)
    and events.zones.zone_map_geometry already falls back to an empty box,
    so a manager who follows a stale link gets a mostly-blank sheet instead
    of a confusing error."""
    performance = _get_org_scoped_performance(request, pk)

    fmt = (request.GET.get("format") or "png").strip().lower()
    size = (request.GET.get("size") or "letter").strip().lower()
    labels = request.GET.get("labels", "1") != "0"
    legend = request.GET.get("legend", "1") != "0"

    try:
        content = render_zone_map(performance, fmt=fmt, size=size, labels=labels, legend=legend)
    except ZoneExportError as exc:
        messages.error(request, str(exc))
        return redirect("dashboard_performance_pricing_zones", pk=performance.pk)

    ext = "pdf" if fmt == "pdf" else "png"
    slug = slugify(f"{performance.event.title}-{performance.starts_at:%Y-%m-%d}") or "performance"
    filename = f"{slug}-pricing-zones.{ext}"

    response = HttpResponse(content, content_type=_EXPORT_CONTENT_TYPES[ext])
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# --- team / roles (manager+, owner-only for the owner role) ---------------


def _assignable_roles(membership):
    """Role values `membership` is allowed to grant. Only owners can hand out
    (or move someone into/out of) the owner role."""
    roles = [Membership.Role.MANAGER, Membership.Role.BOX_OFFICE, Membership.Role.SCANNER]
    if membership.is_owner():
        roles = [Membership.Role.OWNER, *roles]
    return [str(r) for r in roles]


def _owner_count(organization):
    return Membership.objects.filter(
        organization=organization, role=Membership.Role.OWNER
    ).count()


def _render_team(request, form=None):
    organization = request.organization
    memberships = (
        Membership.objects.filter(organization=organization)
        .select_related("user")
        .order_by("role", "user__email")
    )
    assignable = _assignable_roles(request.membership)
    if form is None:
        form = InviteMemberForm(allowed_roles=assignable)
    return render(
        request,
        "dashboard/team.html",
        {
            "memberships": memberships,
            "form": form,
            "assignable_roles": assignable,
            "role_choices": Membership.Role.choices,
            "my_membership_id": request.membership.id,
        },
    )


@manager_required
def team(request):
    """List staff and their roles. Any manager+ can view; mutations go through
    the POST handlers below, which re-check the owner-role gate server-side."""
    return _render_team(request)


@manager_required
@require_POST
def team_add(request):
    organization = request.organization
    assignable = _assignable_roles(request.membership)
    form = InviteMemberForm(request.POST, allowed_roles=assignable)
    if not form.is_valid():
        return _render_team(request, form=form)

    role = form.cleaned_data["role"]
    if role not in assignable:
        # Belt-and-suspenders: the form already limits choices to `assignable`,
        # but re-check so a hand-crafted POST can't grant a role above the
        # actor's own authority (e.g. a manager minting an owner).
        messages.error(request, "You can't assign that role.")
        return redirect("dashboard_team")

    try:
        _membership, created_user, invite_sent = add_member(
            organization=organization,
            email=form.cleaned_data["email"],
            role=role,
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            request=request,
        )
    except MemberExistsError:
        form.add_error("email", "That person is already on this team.")
        return _render_team(request, form=form)

    if invite_sent:
        messages.success(
            request,
            f"Invited {form.cleaned_data['email']} — they've been emailed a link to set a password.",
        )
    else:
        messages.success(
            request,
            f"Added {form.cleaned_data['email']} to the team. They sign in with their existing password.",
        )
    return redirect("dashboard_team")


@manager_required
@require_POST
def team_update_role(request, pk):
    organization = request.organization
    actor = request.membership
    target = get_object_or_404(Membership, pk=pk, organization=organization)
    new_role = request.POST.get("role")

    if new_role not in Membership.Role.values:
        messages.error(request, "Unknown role.")
        return redirect("dashboard_team")

    if target.id == actor.id:
        messages.error(request, "You can't change your own role.")
        return redirect("dashboard_team")

    # Owner-role changes (promoting to owner, or demoting an existing owner)
    # are owner-only.
    touches_owner = target.is_owner() or new_role == Membership.Role.OWNER
    if touches_owner and not actor.is_owner():
        messages.error(request, "Only an owner can grant or change the owner role.")
        return redirect("dashboard_team")

    # Never leave the organization with no owner.
    if target.is_owner() and new_role != Membership.Role.OWNER and _owner_count(organization) <= 1:
        messages.error(request, "This is the only owner — promote someone else first.")
        return redirect("dashboard_team")

    if target.role != new_role:
        target.role = new_role
        target.save(update_fields=["role"])
        messages.success(request, f"Updated {target.user.email} to {target.get_role_display()}.")
    return redirect("dashboard_team")


@manager_required
@require_POST
def team_remove(request, pk):
    organization = request.organization
    actor = request.membership
    target = get_object_or_404(Membership, pk=pk, organization=organization)

    if target.id == actor.id:
        messages.error(request, "You can't remove yourself.")
        return redirect("dashboard_team")

    if target.is_owner() and not actor.is_owner():
        messages.error(request, "Only an owner can remove an owner.")
        return redirect("dashboard_team")

    if target.is_owner() and _owner_count(organization) <= 1:
        messages.error(request, "This is the only owner — you can't remove them.")
        return redirect("dashboard_team")

    email = target.user.email
    # Remove the Membership only, not the User: they may belong to other
    # organizations (accounts.User is global, membership is per-org).
    target.delete()
    messages.success(request, f"Removed {email} from the team.")
    return redirect("dashboard_team")


# --- audience / CRM (manager+, Phase 4) -------------------------------------
#
# The guest list + per-guest detail. Mirrors the donations/passes report
# shape (search/filter GET params, `?format=csv` export) rather than a plain
# ListView, since audience_queryset (campaigns.services) already does all the
# filtering/annotation work -- this view is a thin GET-param-to-kwargs
# translation over it, same division of labor as donations_report/pass_report
# over their own OrderItem querysets.


@manager_required
def audience_list(request):
    organization = request.organization
    search = request.GET.get("search", "").strip()
    tag = request.GET.get("tag", "").strip()
    # opt_in is a tri-state GET param ("" = everyone, "1" = opted in, "0" =
    # opted out) -- translated to audience_queryset's True/False/None kwarg.
    opt_in_param = request.GET.get("opt_in", "").strip()
    opt_in = {"1": True, "0": False}.get(opt_in_param)

    guests = audience_queryset(organization, search=search, opt_in=opt_in, tag=tag)

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="audience.csv"'
        writer = csv.writer(response)
        writer.writerow(["Email", "Name", "Opted in", "Orders", "Lifetime value", "Tags"])
        for guest in guests:
            writer.writerow(
                [
                    guest.email,
                    guest.name,
                    "yes" if guest.marketing_opt_in else "no",
                    guest.order_count,
                    guest.ltv or Decimal("0.00"),
                    guest.tags,
                ]
            )
        return response

    return render(
        request,
        "dashboard/audience_list.html",
        {"guests": guests, "search": search, "tag": tag, "opt_in": opt_in_param},
    )


@manager_required
def audience_detail(request, pk):
    """One guest's CRM record: order/pass history (read-only) plus the
    editable tags/notes form. Consent itself is never editable here -- see
    GuestTagsNotesForm's docstring -- only the guest's own portal toggle or
    the unsubscribe link can change marketing_opt_in."""
    organization = request.organization
    guest = get_object_or_404(GuestAccount.objects.for_organization(organization), pk=pk)

    if request.method == "POST":
        form = GuestTagsNotesForm(request.POST, instance=guest)
        if form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect("dashboard_audience_detail", pk=guest.pk)
    else:
        form = GuestTagsNotesForm(instance=guest)

    # Same order-history query shape as guests.views.guest_portal's own "My
    # tickets" list -- this is the staff-facing mirror of that self-service
    # view, over the same rows.
    orders = (
        Order.objects.for_organization(organization)
        .filter(guest=guest)
        .select_related("performance", "performance__event", "performance__venue")
        .prefetch_related("tickets")
        .order_by("-created_at")
    )
    order_rows = [{"order": order, "ticket_count": order.tickets.count()} for order in orders]

    pass_rows = list(
        PassPurchase.objects.filter(organization=organization, guest=guest)
        .select_related("product")
        .order_by("-created_at")
    )

    return render(
        request,
        "dashboard/audience_detail.html",
        {"guest": guest, "form": form, "order_rows": order_rows, "pass_rows": pass_rows},
    )


# --- email campaigns (manager+, Phase 4) ------------------------------------
#
# Mirrors the pass-product CRUD shape (flat list/create/edit CBVs) with one
# difference: a campaign is only editable while DRAFT (EmailCampaignUpdateView's
# get_queryset), since triggering it (start_campaign) fixes its content as
# sent history -- there's no is_active toggle here, a campaign's STATUS
# lifecycle (draft -> sending -> sent/cancelled) already gates everything.


class EmailCampaignListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/campaign_list.html"
    context_object_name = "campaigns"

    def get_queryset(self):
        return EmailCampaign.objects.filter(organization=self.request.organization).order_by(
            "-created_at"
        )


class EmailCampaignCreateView(ManagerRequiredMixin, CreateView):
    model = EmailCampaign
    form_class = EmailCampaignForm
    template_name = "dashboard/campaign_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        form.instance.organization = self.request.organization
        form.instance.created_by = self.request.user
        messages.success(self.request, f"Created “{form.instance.name}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_campaign_detail", args=[self.object.pk])


class EmailCampaignUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = EmailCampaignForm
    template_name = "dashboard/campaign_form.html"

    def get_queryset(self):
        # Draft-only editable: once a campaign has been triggered
        # (start_campaign flips it SENDING) its content is fixed send
        # history, mirrored by CampaignForm's own "only DRAFT" gate server-
        # side -- a pk for a non-draft campaign 404s here rather than
        # silently allowing an edit that can no longer affect what was sent.
        return EmailCampaign.objects.filter(
            organization=self.request.organization, status=EmailCampaign.Status.DRAFT
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Updated “{form.instance.name}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_campaign_detail", args=[self.object.pk])


@manager_required
def campaign_detail(request, pk):
    """Campaign summary: sent/failed/skipped/pending counts over its
    CampaignSend rows, plus the failed list (what a manager needs to
    investigate a bad send) and, while still DRAFT, a live recipient-count
    preview for the send-confirmation control (campaign_send's template)."""
    campaign = get_object_or_404(
        EmailCampaign.objects.filter(organization=request.organization), pk=pk
    )
    counts = campaign.sends.aggregate(
        sent=Count("id", filter=Q(status=CampaignSend.Status.SENT)),
        failed=Count("id", filter=Q(status=CampaignSend.Status.FAILED)),
        skipped=Count("id", filter=Q(status=CampaignSend.Status.SKIPPED)),
        pending=Count(
            "id",
            filter=Q(status__in=[CampaignSend.Status.PENDING, CampaignSend.Status.SENDING]),
        ),
    )
    failed_sends = list(
        campaign.sends.filter(status=CampaignSend.Status.FAILED)
        .select_related("guest")
        .order_by("-created_at")[:200]
    )
    recipient_preview = None
    if campaign.status == EmailCampaign.Status.DRAFT:
        recipient_preview = segment_recipient_count(campaign)

    return render(
        request,
        "dashboard/campaign_detail.html",
        {
            "campaign": campaign,
            "counts": counts,
            "failed_sends": failed_sends,
            "recipient_preview": recipient_preview,
        },
    )


@manager_required
def campaign_preview(request, pk):
    """Live recipient-count endpoint the composer's fetch() hits (see
    campaign_form.html) -- the exact same segment_recipient_count the send
    confirmation and the eventual fan-out use, so the number shown while
    composing never disagrees with what start_campaign actually queues."""
    campaign = get_object_or_404(
        EmailCampaign.objects.filter(organization=request.organization), pk=pk
    )
    return JsonResponse({"count": segment_recipient_count(campaign)})


@manager_required
@require_POST
def campaign_test(request, pk):
    """Send a one-off preview of the campaign to the acting staffer's own
    email (send_test_campaign_email) -- no CampaignSend rows, no status
    change; see that function's docstring."""
    campaign = get_object_or_404(
        EmailCampaign.objects.filter(organization=request.organization), pk=pk
    )
    if not request.user.email:
        messages.error(request, "Your account has no email address to send a test to.")
        return redirect("dashboard_campaign_detail", pk=campaign.pk)
    try:
        send_test_campaign_email(campaign, request.user.email)
    except Exception:  # delivery/transport failure -- don't 500 the dashboard
        messages.error(request, "Couldn't send the test email just now. Please try again.")
    else:
        messages.success(request, f"Sent a test email to {request.user.email}.")
    return redirect("dashboard_campaign_detail", pk=campaign.pk)


@manager_required
@require_POST
def campaign_send(request, pk):
    """Trigger the campaign (campaigns.services.start_campaign): materializes
    its segment into PENDING CampaignSend rows and flips DRAFT -> SENDING for
    the cron batch sender to work through. CampaignStateError (already
    sending/sent/cancelled -- e.g. a double-click) is caught and flashed
    rather than 500ing; the confirm step lives in the template (a JS confirm()
    naming the live recipient count from campaign_detail's preview) since the
    actual trigger here is a single idempotent-enough POST."""
    campaign = get_object_or_404(
        EmailCampaign.objects.filter(organization=request.organization), pk=pk
    )
    try:
        count = start_campaign(campaign)
    except CampaignStateError as exc:
        messages.error(request, str(exc))
        return redirect("dashboard_campaign_detail", pk=campaign.pk)
    messages.success(request, f"Queued {count} recipient{'s' if count != 1 else ''}.")
    return redirect("dashboard_campaign_detail", pk=campaign.pk)
