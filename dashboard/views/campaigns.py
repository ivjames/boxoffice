from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from accounts.permissions import ManagerRequiredMixin, manager_required
from campaigns.emails import send_test_campaign_email
from campaigns.models import CampaignSend, EmailCampaign
from campaigns.services import CampaignStateError, segment_recipient_count, start_campaign

from ..forms import EmailCampaignForm


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
