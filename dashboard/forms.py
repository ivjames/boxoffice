"""ModelForms for the dashboard's Event/Performance/PriceTier CRUD. Every
form takes the acting `organization` explicitly (never trusts a hidden
field) and scopes/sets it itself, so a manager can never point a save at
another tenant's data no matter what a form POST contains -- see
docs/ARCHITECTURE.md "Tenant isolation is non-negotiable."
"""

from decimal import Decimal, InvalidOperation

from django import forms

from accounts.models import Membership
from campaigns.models import EmailCampaign
from donations.models import DonationCampaign
from events.models import Event, GAAllocation, Performance, PriceTier
from guests.models import GuestAccount
from orders.services import get_seating_chart
from passes.models import PassProduct
from promotions.models import PromoCode
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
        fields = ["name", "tier", "numbering_scheme", "row_label_scheme"]

    def __init__(self, *args, organization, chart, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.chart = chart

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


# --- promo codes (manager+) -------------------------------------------------


class PromoCodeForm(forms.ModelForm):
    """Dashboard CRUD for promotions.PromoCode -- see its docstring for the
    field semantics (percent vs fixed, the soft max_redemptions cap,
    redemption accounting). `organization` is taken explicitly (never
    trusted from POST data), purely so clean_code can run its per-org
    uniqueness check during validation -- the CreateView is what actually
    STAMPS instance.organization on create (mirrors EventCreateView); this
    form never writes it itself, same division of responsibility as every
    other CRUD form in this module."""

    class Meta:
        model = PromoCode
        fields = [
            "code", "kind", "value", "currency", "starts_at", "ends_at",
            "max_redemptions", "min_order_amount", "is_active",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "ends_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["starts_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["ends_at"].input_formats = ["%Y-%m-%dT%H:%M"]

    def clean_code(self):
        # `organization` isn't a form field (it's fixed by the view, not
        # user-editable here), so Django's own ModelForm.validate_unique()
        # excludes it -- and with it, the WHOLE unique_promo_code_per_org
        # (organization, code) constraint check (a model field absent from
        # the form can't have its unique-together validated, by design --
        # see BaseModelForm._get_validation_exclusions()). Left unchecked, a
        # duplicate code under THIS org would pass form validation and blow
        # up as a raw IntegrityError at PromoCode.save() instead of a clean
        # field error. Mirrors SectionForm.clean_name's approach above.
        # Normalized the same way PromoCode.save() normalizes (strip/upper)
        # so this check matches exactly what will actually be persisted.
        code = (self.cleaned_data.get("code") or "").strip().upper()
        if code:
            existing = PromoCode.objects.filter(organization=self.organization, code=code)
            if self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError("A promo code with this code already exists.")
        return code

    def clean(self):
        # Business-rule bounds on `value` that depend on `kind`, so they
        # can't live as a plain field validator: a PERCENT code above 100 (or
        # below 0) is nonsensical, and a FIXED code has to actually discount
        # something -- promotions.services.validate_code separately refuses
        # a discount that would zero out or exceed the cart, but that's a
        # per-order check made at apply time; this is the simpler "is this
        # code well-formed at all" check made at save time.
        cleaned = super().clean()
        kind = cleaned.get("kind")
        value = cleaned.get("value")
        if value is not None:
            if kind == PromoCode.Kind.PERCENT and not (Decimal("0") <= value <= Decimal("100")):
                self.add_error("value", "A percentage must be between 0 and 100.")
            elif kind == PromoCode.Kind.FIXED and value <= Decimal("0"):
                self.add_error("value", "A fixed amount must be greater than 0.")
        return cleaned


# --- donations (manager+) --------------------------------------------------
#
# v1 is a single org-wide campaign (see DonationCampaign's docstring), so
# there's no create/list CRUD here the way promo codes get one -- just a
# settings form against the org's one campaign row, dashboard.views.
# donation_settings loads it via get_or_create_general_fund and saves this
# form onto it.


class PassProductForm(forms.ModelForm):
    """Dashboard CRUD for passes.PassProduct -- see its docstring for the
    kind/credit_count/valid-window/events semantics. `organization` is taken
    explicitly (never trusted from POST data), purely to scope the `events`
    field's queryset to this tenant's own events -- the view is what actually
    stamps instance.organization on create (mirrors PromoCodeForm/EventForm),
    this form never writes it itself."""

    class Meta:
        model = PassProduct
        fields = [
            "name", "kind", "price", "credit_count",
            "valid_from", "valid_until", "events", "is_active",
        ]
        widgets = {
            "valid_from": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "valid_until": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "events": forms.CheckboxSelectMultiple,
        }
        help_texts = {
            "credit_count": "Flex passes only -- how many admission credits the pass grants. Leave blank for a season pass.",
            "events": "Leave every box unchecked for an all-access pass (covers every event). Check specific events to restrict coverage to just those.",
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["valid_from"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["valid_until"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["events"].queryset = Event.objects.filter(organization=organization).order_by(
            "title"
        )
        self.fields["events"].required = False
        self.fields["credit_count"].required = False

    def clean(self):
        # The flex/season credit_count shape mirrors the DB's own
        # passproduct_credit_shape CheckConstraint (PassProduct.Meta) -- caught
        # here as a clean field error instead of a raw IntegrityError at save().
        cleaned = super().clean()
        kind = cleaned.get("kind")
        credit_count = cleaned.get("credit_count")
        price = cleaned.get("price")
        if kind == PassProduct.Kind.FLEX:
            if not credit_count or credit_count <= 0:
                self.add_error("credit_count", "A flex pass needs a positive credit count.")
        elif kind == PassProduct.Kind.SEASON:
            if credit_count:
                self.add_error(
                    "credit_count", "A season pass doesn't use credits -- leave this blank."
                )
        if price is not None and price < 0:
            self.add_error("price", "Price can't be negative.")
        return cleaned


class DonationSettingsForm(forms.ModelForm):
    """Dashboard settings form for the org's single DonationCampaign.
    `is_active` doubles as the donations on/off switch for the whole tenant
    (storefront nav link, cart add-on, /donate/ page -- see
    DonationCampaign's docstring); `suggested_amounts` is the quick-pick CSV
    validated below against DonationCampaign.suggested_amount_list's own
    lenient parse, so a manager gets a clean field error instead of silently
    losing a mistyped entry the model would just skip."""

    class Meta:
        model = DonationCampaign
        fields = ["is_active", "name", "suggested_amounts", "acknowledgment"]
        widgets = {
            "acknowledgment": forms.Textarea(attrs={"rows": 4}),
        }

    def clean_suggested_amounts(self):
        # Mirrors DonationCampaign.suggested_amount_list's own parse (comma-
        # split, strip, Decimal, positive) so a manager who fat-fingers this
        # field (blank, non-numeric, negative -- or an all-blank/empty CSV
        # that would leave the storefront with NO preset buttons) sees a
        # clear validation error here instead of the model silently skipping
        # the bad entries at render time.
        raw = self.cleaned_data.get("suggested_amounts", "")
        amounts = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                value = Decimal(chunk)
            except InvalidOperation:
                raise forms.ValidationError(f"“{chunk}” isn't a valid amount.")
            if value <= 0:
                raise forms.ValidationError(f"“{chunk}” must be a positive amount.")
            amounts.append(chunk)
        if not amounts:
            raise forms.ValidationError("Enter at least one positive amount, e.g. 10,25,50,100.")
        return ",".join(amounts)


# --- CRM / email marketing (manager+, Phase 4) ------------------------------
#
# Mirrors PassProductForm's shape: `organization` is taken explicitly (never
# trusted from POST data), purely to scope the `segment_event` field's
# queryset to this tenant's own events -- the view stamps instance.organization
# (and created_by) on create, this form never writes either itself.


class EmailCampaignForm(forms.ModelForm):
    """Dashboard CRUD for campaigns.EmailCampaign -- see its docstring for the
    lifecycle/segment semantics. clean() enforces that the segment param
    matching the chosen segment_kind is actually filled in, mirroring
    PassProductForm.clean()'s kind-conditional-field pattern; the other
    segment param is left as submitted (harmless -- campaigns.services.
    segment_guests only reads the param relevant to the campaign's own kind)."""

    class Meta:
        model = EmailCampaign
        fields = [
            "name", "subject", "body",
            "segment_kind", "segment_event", "segment_min_spend",
        ]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 10}),
        }
        help_texts = {
            "body": "Plain text, paragraphs separated by a blank line -- the HTML email is generated from this automatically.",
            "segment_event": "Required when the segment is “Bought a specific event”.",
            "segment_min_spend": "Required when the segment is “Minimum lifetime spend”.",
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["segment_event"].queryset = Event.objects.filter(organization=organization).order_by(
            "title"
        )
        self.fields["segment_event"].required = False
        self.fields["segment_min_spend"].required = False

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("segment_kind")
        if kind == EmailCampaign.SegmentKind.EVENT and not cleaned.get("segment_event"):
            self.add_error("segment_event", "Choose the event this campaign targets.")
        if kind == EmailCampaign.SegmentKind.MIN_SPEND and not cleaned.get("segment_min_spend"):
            self.add_error("segment_min_spend", "Enter a minimum lifetime spend amount.")
        return cleaned


class GuestTagsNotesForm(forms.ModelForm):
    """The audience detail page's editable staff fields on a guest -- tags
    (a free-text CSV, GuestAccount.tag_list parses it) and private notes.
    Everything else about a GuestAccount (email, opt-in state, order history)
    is read-only here; consent is only ever changed by the guest themselves
    (portal toggle / unsubscribe link), never by staff."""

    class Meta:
        model = GuestAccount
        fields = ["tags", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "tags": "Comma-separated, e.g. vip, subscriber, board member.",
        }
