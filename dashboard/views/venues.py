import json

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts.permissions import ManagerRequiredMixin, manager_required
from venues import chart_parsing, generation
from venues.models import ChartParseJob, Seat, SeatingChart, Section, Venue

from ..forms import SeatingChartForm, SectionForm
from ._common import _section_color


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
