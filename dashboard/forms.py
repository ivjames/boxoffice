"""ModelForms for the dashboard's Event/Performance/PriceTier CRUD. Every
form takes the acting `organization` explicitly (never trusts a hidden
field) and scopes/sets it itself, so a manager can never point a save at
another tenant's data no matter what a form POST contains -- see
docs/ARCHITECTURE.md "Tenant isolation is non-negotiable."
"""

from django import forms

from events.models import Event, GAAllocation, Performance, PriceTier
from orders.services import get_seating_chart
from venues.models import Section, Venue


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
        fields = ["venue", "starts_at", "seating_mode", "status"]
        widgets = {
            "starts_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            )
        }

    def __init__(self, *args, organization, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.event = event
        self.fields["venue"].queryset = Venue.objects.filter(organization=organization)
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
