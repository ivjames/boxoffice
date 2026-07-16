from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts.permissions import ManagerRequiredMixin, box_office_required, manager_required
from events.models import Event, Performance, PriceTier
from orders.models import Order, PerformanceSeatBlock, Ticket
from orders.services import get_seating_chart, performance_seats
from venues.models import Section

from ..forms import EventForm, PerformanceForm, PriceTierForm


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
