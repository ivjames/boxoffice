import json
import uuid
from decimal import Decimal, InvalidOperation

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

from accounts.permissions import BoxOfficeRequiredMixin, ManagerRequiredMixin, manager_required, tenant_staff_required
from events import zones as zone_services
from events.models import Event, Performance, PriceTier, PricingZone, ZoneTemplate
from events.zone_export import ZoneExportError, render_zone_map
from orders.models import Order, Ticket
from orders.services import get_seating_chart, performance_seats
from venues import generation
from venues.models import Seat, SeatingChart, Section, Venue

from .forms import EventForm, PerformanceForm, PriceTierForm, SeatingChartForm, SectionForm


# --- overview / reports ---------------------------------------------------


@tenant_staff_required
def overview(request):
    """Any staff role can view the overview. Counts + reports are all scoped
    to request.organization -- see accounts.permissions for how every
    dashboard view gets there (login + Membership-in-this-org check)."""
    organization = request.organization
    now = timezone.now()

    upcoming_performances = list(
        Performance.objects.filter(organization=organization, starts_at__gte=now)
        .select_related("event", "venue")
        .order_by("starts_at")[:10]
    )

    tickets_sold = (
        Ticket.objects.filter(organization=organization).exclude(status=Ticket.Status.VOID).count()
    )
    gross_revenue = (
        Order.objects.filter(organization=organization, status=Order.Status.PAID).aggregate(
            total=Sum("total")
        )["total"]
        or Decimal("0.00")
    )

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
        performance_rows.append({"performance": performance, "sold": sold, "capacity": capacity})

    context = {
        "upcoming_performances": upcoming_performances,
        "tickets_sold": tickets_sold,
        "gross_revenue": gross_revenue,
        "performance_rows": performance_rows,
    }
    return render(request, "dashboard/overview.html", context)


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
            filters = Q(buyer_email__icontains=query) | Q(buyer_name__icontains=query)
            try:
                filters |= Q(token=uuid.UUID(query))
            except ValueError:
                pass
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
        return context


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
        return context


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
    "numbering_scheme", "row_label_scheme", "pivot_mode", "pivot_x", "pivot_y",
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
