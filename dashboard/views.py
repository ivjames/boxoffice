import json
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts.permissions import BoxOfficeRequiredMixin, ManagerRequiredMixin, manager_required, tenant_staff_required
from events import zones as zone_services
from events.models import Event, Performance, PriceTier, PricingZone, ZoneTemplate
from orders.models import Order, Ticket
from orders.services import get_seating_chart, performance_seats
from venues import generation
from venues.models import Seat, SeatingChart, Section, Venue

from .forms import EventForm, GenerateSeatsForm, PerformanceForm, PriceTierForm, SeatingChartForm, SectionForm


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

    if performance.seating_mode == Performance.SeatingMode.GA:
        tiers = performance.price_tiers.all()
    else:
        chart = get_seating_chart(performance)
        tiers = PriceTier.objects.filter(organization=request.organization, section__chart=chart)

    return render(
        request,
        "dashboard/performance_price_tiers.html",
        {"performance": performance, "form": form, "tiers": tiers},
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
    model = Section
    form_class = SectionForm
    template_name = "dashboard/section_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.chart = get_object_or_404(
            SeatingChart, pk=kwargs["chart_pk"], organization=request.organization
        )
        return super().dispatch(request, *args, **kwargs)

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
        messages.success(self.request, "Section created.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_section_detail", args=[self.chart.pk, self.object.pk])


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
        messages.success(self.request, "Section layout updated.")
        return super().form_valid(form)

    def get_success_url(self):
        # The visual editor's "Edit layout" link sends staff here with
        # ?next=editor (rendered as a hidden field on submit -- see
        # section_form.html) so saving comes back to the editor instead of
        # section_detail. Whitelisted to this one literal value -- never an
        # arbitrary redirect target from user input.
        if self.request.POST.get("next") == "editor":
            return reverse("dashboard_chart_editor", args=[self.chart.pk])
        return reverse("dashboard_section_detail", args=[self.chart.pk, self.object.pk])


def _get_org_scoped_section(request, chart_pk, section_pk):
    chart = get_object_or_404(SeatingChart, pk=chart_pk, organization=request.organization)
    section = get_object_or_404(
        Section, pk=section_pk, chart=chart, organization=request.organization
    )
    return chart, section


@manager_required
def section_detail(request, chart_pk, pk):
    """Generate-seats form (venues.generation.generate_seats) + a basic
    read-only-except-toggle/delete preview of the section's current seats,
    laid out on the same CSS grid the storefront seat map uses (seat.x/y),
    so staff can see ragged rows / accessible flags / aisle gaps at a
    glance without needing the Phase B visual editor."""
    chart, section = _get_org_scoped_section(request, chart_pk, pk)

    if request.method == "POST":
        form = GenerateSeatsForm(request.POST)
        if form.is_valid():
            row_counts = form.cleaned_data["row_counts"]
            try:
                generation.generate_seats(
                    section, row_counts, replace=form.cleaned_data["replace_existing"]
                )
            except generation.SeatGenerationError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Generated {sum(row_counts)} seat(s).")
                return redirect("dashboard_section_detail", chart_pk=chart.pk, pk=section.pk)
    else:
        form = GenerateSeatsForm()

    seats = list(section.seats.order_by("row_label", "number"))
    seat_cells = [
        {"seat": seat, "grid_x": int(round(seat.x)) + 1, "grid_y": int(round(seat.y)) + 1}
        for seat in seats
    ]
    max_x = max((cell["grid_x"] for cell in seat_cells), default=1)
    max_y = max((cell["grid_y"] for cell in seat_cells), default=1)

    return render(
        request,
        "dashboard/section_detail.html",
        {
            "chart": chart,
            "section": section,
            "form": form,
            "seat_cells": seat_cells,
            "max_x": max_x,
            "max_y": max_y,
        },
    )


@manager_required
@require_POST
def seat_toggle_accessible(request, chart_pk, section_pk, seat_pk):
    chart, section = _get_org_scoped_section(request, chart_pk, section_pk)
    seat = get_object_or_404(
        Seat, pk=seat_pk, section=section, organization=request.organization
    )
    seat.is_accessible = not seat.is_accessible
    seat.save(update_fields=["is_accessible"])
    return redirect("dashboard_section_detail", chart_pk=chart.pk, pk=section.pk)


@manager_required
@require_POST
def seat_delete(request, chart_pk, section_pk, seat_pk):
    """Remove a single seat (e.g. to open up an aisle gap). Refused if the
    seat backs a live (non-void) ticket -- same "never orphan an issued
    ticket" rule venues.generation.generate_seats enforces in bulk."""
    chart, section = _get_org_scoped_section(request, chart_pk, section_pk)
    seat = get_object_or_404(
        Seat, pk=seat_pk, section=section, organization=request.organization
    )
    if Ticket.objects.filter(seat=seat).exclude(status=Ticket.Status.VOID).exists():
        messages.error(request, f"Can't remove {seat} -- it has a live ticket issued.")
    else:
        label = str(seat)
        seat.delete()
        messages.success(request, f"Removed seat {label}.")
    return redirect("dashboard_section_detail", chart_pk=chart.pk, pk=section.pk)


# --- seating chart visual editor (Phase B, docs/SEATING.md) ---------------
#
# SVG drag editor: renders every section/seat of a chart with true (float,
# unrounded) x/y -- unlike section_detail's CSS-grid preview above, which
# rounds to integer cells for a simple `<div>` grid, the editor needs real
# geometry for raked/fanned shapes to render/drag correctly. Dragging itself
# is client-side (Alpine + pointer events on inline SVG -- no canvas
# library, per the epic's locked "SVG drag" decision); chart_editor_save
# below is the only thing that persists it, and it's the sole place a
# manager can move a seat off its generated position. Phase C's drag-select
# pricing zones can reuse the same <svg>/seat-element structure (each seat
# is `<circle class="editor-seat" data-seat-id data-section-id ...>` --
# see the template) for marquee/shift-click selection instead of building a
# second seat-map renderer.

_SECTION_PALETTE = [
    "#e11d48", "#2563eb", "#059669", "#d97706", "#7c3aed",
    "#0891b2", "#db2777", "#65a30d", "#4f46e5", "#dc2626",
]


def _section_color(index):
    return _SECTION_PALETTE[index % len(_SECTION_PALETTE)]


@manager_required
def chart_editor(request, pk):
    """GET-only: renders the whole chart as one inline SVG. Seat/section
    data is embedded via json_script (same pattern as
    templates/orders/_seat_map.html) so the Alpine component owns drag
    state client-side; chart_editor_save is a separate POST endpoint, not a
    Django form post, since it batches every dragged seat in one request."""
    chart = get_object_or_404(
        SeatingChart.objects.select_related("venue"), pk=pk, organization=request.organization
    )
    sections = list(chart.sections.order_by("ordering", "name"))
    seats = list(
        Seat.objects.filter(organization=request.organization, section__chart=chart)
        .select_related("section")
        .order_by("section__ordering", "section__name", "row_label", "number")
    )

    section_color_by_id = {section.pk: _section_color(i) for i, section in enumerate(sections)}
    for section in sections:
        # Not persisted -- render-time convenience so chart_editor.html can
        # set swatch/seat fill colors without a dict lookup in Django
        # template syntax (which can't do `dict[var]` cleanly).
        section.editor_color = section_color_by_id[section.pk]
    for seat in seats:
        seat.editor_color = section_color_by_id.get(seat.section_id, "#6b7280")

    sections_json = [
        {
            "id": section.pk,
            "name": section.name,
            "tier": section.tier,
            "color": section_color_by_id[section.pk],
            "seat_count": sum(1 for s in seats if s.section_id == section.pk),
            "rotation": section.rotation,
            "seat_pitch": section.seat_pitch,
            "row_pitch": section.row_pitch,
            "row_x_offset": section.row_x_offset,
            "arc_radius": section.arc_radius,
        }
        for i, section in enumerate(sections)
    ]
    seats_json = [
        {
            "id": seat.pk,
            "section_id": seat.section_id,
            "row": seat.row_label,
            "number": seat.number,
            "x": seat.x,
            "y": seat.y,
            "accessible": seat.is_accessible,
            "color": section_color_by_id.get(seat.section_id, "#6b7280"),
        }
        for seat in seats
    ]

    xs = [s["x"] for s in seats_json]
    ys = [s["y"] for s in seats_json]
    pitches = [section.seat_pitch for section in sections if section.seat_pitch] or [1.0]
    seat_radius = max(0.15, min(pitches) * 0.35)
    pad = seat_radius * 4 + 1
    if xs and ys:
        view_min_x, view_max_x = min(xs) - pad, max(xs) + pad
        view_min_y, view_max_y = min(ys) - pad, max(ys) + pad
    else:
        view_min_x = view_min_y = 0.0
        view_max_x = view_max_y = 10.0
    view_box = f"{view_min_x} {view_min_y} {view_max_x - view_min_x} {view_max_y - view_min_y}"

    return render(
        request,
        "dashboard/chart_editor.html",
        {
            "chart": chart,
            "sections": sections,
            "seats": seats,
            "sections_json": sections_json,
            "seats_json": seats_json,
            "view_box": view_box,
            "seat_radius": seat_radius,
            "save_url": reverse("dashboard_chart_editor_save", args=[chart.pk]),
        },
    )


@manager_required
@require_POST
def chart_editor_save(request, pk):
    """Batch-persist dragged seat positions: JSON body
    `{"positions": {seat_id: {"x": ..., "y": ...}, ...}}`. Org- AND
    chart-scoped: a seat id that doesn't belong to THIS organization's THIS
    chart is silently excluded from the update queryset below -- never
    mutated, never even distinguished from "doesn't exist" in the response,
    so this can't be used to probe another tenant's seat ids either.
    Repositioning is cosmetic (position != identity, unlike seat_delete/
    generate_seats above), so this is allowed even for sold/ticketed seats
    -- role + tenant scoping are the only gates, per docs/SEATING.md Phase B."""
    chart = get_object_or_404(SeatingChart, pk=pk, organization=request.organization)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    positions = payload.get("positions")
    if not isinstance(positions, dict) or not positions:
        return JsonResponse({"ok": False, "error": "No positions given."}, status=400)

    try:
        seat_ids = [int(seat_id) for seat_id in positions.keys()]
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid seat id."}, status=400)

    # The tenant-isolation gate: filtered by BOTH organization and this
    # chart, so a seat id for another org (or another chart in this org)
    # just isn't in `seats` below -- silently excluded, never touched.
    seats = list(
        Seat.objects.filter(
            pk__in=seat_ids, organization=request.organization, section__chart=chart
        )
    )

    updated = []
    skipped = 0
    for seat in seats:
        raw = positions.get(str(seat.pk))
        try:
            x = float(raw["x"])
            y = float(raw["y"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        seat.x = x
        seat.y = y
        updated.append(seat)

    if updated:
        Seat.objects.bulk_update(updated, ["x", "y"])
    skipped += len(seat_ids) - len(seats)  # ids not in this org/chart at all

    return JsonResponse({"ok": True, "updated": len(updated), "skipped": skipped})


@manager_required
@require_POST
def section_regenerate(request, chart_pk, section_pk):
    """"Regenerate seats (same shape)" from the visual editor: re-runs
    venues.generation.generate_seats with the section's CURRENT layout
    params (whatever was last saved via the existing section edit-layout
    form -- dashboard_section_update) and the row/seat-count shape derived
    from its EXISTING seats, so a manager can tweak rotation/pitch/
    arc_radius, hit this, and see the new geometry applied to the same
    logical rows -- then fine-tune by dragging. Same guardrail as the Phase
    A generate form: refuses outright if any seat has a live ticket.
    Accessible flags are NOT preserved -- matches the existing "replace"
    semantics elsewhere in this module (regenerating is a start-over-the-
    geometry action, not a merge)."""
    chart, section = _get_org_scoped_section(request, chart_pk, section_pk)
    existing = list(section.seats.order_by("row_label", "number"))
    if not existing:
        messages.error(request, f"{section.name} has no seats yet -- generate seats first.")
        return redirect("dashboard_chart_editor", pk=chart.pk)

    counts_by_label = {}
    seen_labels = []
    for seat in existing:
        if seat.row_label not in counts_by_label:
            counts_by_label[seat.row_label] = 0
            seen_labels.append(seat.row_label)
        counts_by_label[seat.row_label] += 1
    row_counts = [counts_by_label[label] for label in seen_labels]

    try:
        generation.generate_seats(section, row_counts, replace=True)
    except generation.SeatGenerationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Regenerated {sum(row_counts)} seat(s) in {section.name}.")
    return redirect("dashboard_chart_editor", pk=chart.pk)


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

    chart = get_seating_chart(performance)
    sections = list(chart.sections.order_by("ordering", "name")) if chart is not None else []
    seats = list(performance_seats(performance).select_related("section"))

    section_color_by_id = {section.pk: _section_color(i) for i, section in enumerate(sections)}
    for seat in seats:
        seat.editor_color = section_color_by_id.get(seat.section_id, "#6b7280")

    xs = [seat.x for seat in seats]
    ys = [seat.y for seat in seats]
    pitches = [section.seat_pitch for section in sections if section.seat_pitch] or [1.0]
    seat_radius = max(0.15, min(pitches) * 0.35)
    pad = seat_radius * 4 + 1
    if xs and ys:
        view_min_x, view_max_x = min(xs) - pad, max(xs) + pad
        view_min_y, view_max_y = min(ys) - pad, max(ys) + pad
    else:
        view_min_x = view_min_y = 0.0
        view_max_x = view_max_y = 10.0
    view_box = f"{view_min_x} {view_min_y} {view_max_x - view_min_x} {view_max_y - view_min_y}"

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
