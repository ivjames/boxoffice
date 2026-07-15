"""ModelForms for the dashboard's Event/Performance/PriceTier CRUD. Every
form takes the acting `organization` explicitly (never trusts a hidden
field) and scopes/sets it itself, so a manager can never point a save at
another tenant's data no matter what a form POST contains -- see
docs/ARCHITECTURE.md "Tenant isolation is non-negotiable."
"""

from django import forms

from accounts.models import Membership
from events.models import Event, GAAllocation, Performance, PriceTier
from orders.services import get_seating_chart
from venues.models import SeatingChart, Section, Venue


class InviteMemberForm(forms.Form):
    """Add-a-teammate form. `allowed_roles` is the set of role values the
    acting staffer may grant (owners can grant any role; managers can't grant
    'owner') -- the view passes it in and re-checks server-side, so tampering
    with the POSTed role can't escalate past what the actor is allowed to
    assign."""

    email = forms.EmailField()
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    role = forms.ChoiceField(choices=Membership.Role.choices)

    def __init__(self, *args, allowed_roles=None, **kwargs):
        super().__init__(*args, **kwargs)
        if allowed_roles is not None:
            allowed = set(allowed_roles)
            self.fields["role"].choices = [
                choice for choice in Membership.Role.choices if choice[0] in allowed
            ]


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ["title", "slug", "description", "category", "image", "status"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class PerformanceForm(forms.ModelForm):
    """`ga_capacity` isn't a Performance field -- it drives an update_or_create
    on the performance's GAAllocation (only meaningful/required when
    seating_mode == GA). Kept on this form rather than a second page so
    creating a GA performance is a single straightforward submit."""

    ga_capacity = forms.IntegerField(
        min_value=0,
        required=False,
        label="GA capacity",
        help_text="Required for General admission performances.",
    )

    class Meta:
        model = Performance
        fields = ["venue", "starts_at", "seating_mode", "seating_chart", "status"]
        widgets = {
            "starts_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            )
        }
        help_texts = {
            "seating_chart": (
                "Optional -- leave blank to use the venue's first seating chart "
                "(orders.services.get_seating_chart's fallback). Only set this once a venue has "
                "more than one chart in play."
            ),
        }

    def __init__(self, *args, organization, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.event = event
        self.fields["venue"].queryset = Venue.objects.filter(organization=organization)
        self.fields["seating_chart"].queryset = SeatingChart.objects.filter(organization=organization)
        self.fields["seating_chart"].required = False
        self.fields["starts_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        if self.instance.pk and self.instance.seating_mode == Performance.SeatingMode.GA:
            allocation = getattr(self.instance, "ga_allocation", None)
            if allocation:
                self.fields["ga_capacity"].initial = allocation.capacity

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("seating_mode") == Performance.SeatingMode.GA:
            capacity = cleaned.get("ga_capacity")
            if capacity is None:
                self.add_error(
                    "ga_capacity", "GA capacity is required for General admission performances."
                )
            elif self.instance.pk:
                allocation = getattr(self.instance, "ga_allocation", None)
                if allocation and capacity < allocation.sold:
                    self.add_error(
                        "ga_capacity",
                        f"Capacity can't be less than the {allocation.sold} ticket(s) already sold.",
                    )
        venue = cleaned.get("venue")
        seating_chart = cleaned.get("seating_chart")
        if venue is not None and seating_chart is not None and seating_chart.venue_id != venue.id:
            self.add_error("seating_chart", "That chart doesn't belong to the selected venue.")
        return cleaned

    def save(self, commit=True):
        performance = super().save(commit=False)
        performance.organization = self.organization
        performance.event = self.event
        if commit:
            performance.save()
            if performance.seating_mode == Performance.SeatingMode.GA:
                GAAllocation.objects.update_or_create(
                    organization=self.organization,
                    performance=performance,
                    defaults={"capacity": self.cleaned_data["ga_capacity"]},
                )
        return performance


class PriceTierForm(forms.ModelForm):
    """A GA performance's tiers hang directly off the Performance (no
    section); a reserved performance's tiers hang off one of its venue's
    seating chart Sections. `section` is dropped from the form entirely for
    GA (there's nothing to choose) rather than hidden, to keep the form
    straightforward."""

    class Meta:
        model = PriceTier
        fields = ["name", "amount", "currency", "section"]

    def __init__(self, *args, organization, performance, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.performance = performance
        if performance.seating_mode == Performance.SeatingMode.GA:
            del self.fields["section"]
        else:
            chart = get_seating_chart(performance)
            self.fields["section"].queryset = Section.objects.filter(
                organization=organization, chart=chart
            )
            self.fields["section"].required = True

    def save(self, commit=True):
        tier = super().save(commit=False)
        tier.organization = self.organization
        if self.performance.seating_mode == Performance.SeatingMode.GA:
            tier.performance = self.performance
            tier.section = None
        else:
            tier.performance = None
        if commit:
            tier.save()
        return tier


# --- seating chart builder (Phase A of the seating-chart epic,
#     docs/SEATING.md) -- manager+, see accounts.permissions.manager_required
#     and dashboard.views' ManagerRequiredMixin usage below. ------------------


class SeatingChartForm(forms.ModelForm):
    class Meta:
        model = SeatingChart
        fields = ["name"]

    def __init__(self, *args, organization, venue, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.venue = venue

    def save(self, commit=True):
        chart = super().save(commit=False)
        chart.organization = self.organization
        chart.venue = self.venue
        if commit:
            chart.save()
        return chart


class SectionForm(forms.ModelForm):
    """Section metadata: name/tier + numbering/row-label scheme.

    Per docs/EDITOR.md's live rework, LAYOUT params (origin, rotation,
    pitch, offset, arc, rows/seats-per-row shape) are no longer edited via
    this form -- they're live-bound sliders/steppers/handles in the chart
    editor canvas (dashboard_chart_editor / chart_editor_save), which is
    also the only place seats actually get (re)generated. This form only
    creates/renames the section shell and picks its numbering conventions.

    `row_label_start` is optional here (missing/blank -> 0, the model
    default) so the inline "New section" modal and any pre-existing caller
    that doesn't send it keep working -- it only matters for houses whose
    row letters continue across tiers (see the model field's help text).

    `ordering` is deliberately NOT a form field (Round-2 feedback,
    docs/EDITOR.md #7): a bare sort-index number input is exactly the kind
    of "raw internal param" the inline "New section" modal shouldn't expose
    -- staff shouldn't have to know or care what integer means "third".
    SectionCreateView auto-assigns it (append to the end of the chart's
    current section list, same append-at-the-end idea as origin_x's
    staggered default); reordering afterward is the chart editor sidebar's
    up/down arrows (dashboard_section_reorder), not this form.
    """

    class Meta:
        model = Section
        fields = ["name", "tier", "numbering_scheme", "row_label_scheme", "row_label_start"]

    def __init__(self, *args, organization, chart, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.chart = chart
        self.fields["row_label_start"].required = False

    def clean_row_label_start(self):
        return self.cleaned_data.get("row_label_start") or 0

    def clean_name(self):
        # `chart` isn't a form field (it's fixed by the URL/view, not user-
        # editable here), so Django's own ModelForm.validate_unique()
        # excludes it -- and with it, the WHOLE unique_section_name_per_chart
        # (chart, name) constraint check (a model field absent from the form
        # can't have its unique-together validated, by design: see
        # BaseModelForm._get_validation_exclusions()). Left unchecked, a
        # duplicate name under THIS chart would pass form validation and
        # blow up as a raw IntegrityError at Section.save() instead of a
        # clean field error -- surfaced by the Round 2 inline "New section"
        # modal (docs/EDITOR.md #7), which needs a real form_invalid()/JSON
        # error response to show a duplicate-name message, not a 500. Doing
        # the check by hand here covers exactly what the DB constraint would
        # have caught.
        name = self.cleaned_data.get("name")
        if name:
            existing = Section.objects.filter(organization=self.organization, chart=self.chart, name=name)
            if self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError("A section with this name already exists on this chart.")
        return name

    def save(self, commit=True):
        section = super().save(commit=False)
        section.organization = self.organization
        section.chart = self.chart
        if commit:
            section.save()
        return section
