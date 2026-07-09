import uuid
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts.permissions import BoxOfficeRequiredMixin, ManagerRequiredMixin, manager_required, tenant_staff_required
from events.models import Event, Performance, PriceTier
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
