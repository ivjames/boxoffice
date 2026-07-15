"""CRM / email-marketing models: a composed EmailCampaign and the per-
recipient CampaignSend log it fans out into.

Phase 4 turns ticket buyers into a mailing list (see docs/ROADMAP.md). The
anchor is guests.GuestAccount -- per-org, email-keyed, already linked to every
Order at fulfillment and now carrying marketing_opt_in consent. This app adds
the two rows the sending pipeline needs:

  EmailCampaign  -- a staff-composed message (subject/body) plus the SEGMENT
                    that selects who receives it and a lifecycle STATUS.
  CampaignSend   -- one row per (campaign, recipient): the delivery log the
                    cron batch sender (campaigns.management.commands.
                    send_campaign_emails) works through, with a snapshot of the
                    recipient's email and a per-row status/error.

Both are TenantScopedModel: a campaign and its sends belong to exactly one
theater, exactly like every other storefront concept. The segment is
materialized ONCE, at trigger time (start_campaign), into concrete
CampaignSend rows -- so the "who got it" list is a durable snapshot, not a
live query that could shift under the batch sender as guests opt in/out mid-
send. See campaigns.services for the segment query and the trigger.

DEPENDENCY DIRECTION (mirrors donations/passes): this app depends on guests
and events; the MONEY path (payments.services) and the dashboard depend on
campaigns, never the reverse. campaigns.services imports only guests/events
models -- never dashboard or views.
"""

from django.conf import settings
from django.db import models

from tenants.models import TenantScopedModel


class EmailCampaign(TenantScopedModel):
    """A staff-composed marketing email plus the audience segment that selects
    its recipients and the lifecycle status that gates sending.

    LIFECYCLE: DRAFT -> SENDING -> SENT (or CANCELLED at any point before it
    finishes). A campaign is composed and previewed as a DRAFT; triggering it
    (campaigns.services.start_campaign) materializes the segment into
    CampaignSend rows, snapshots recipient_count, and flips it to SENDING; the
    cron batch sender works through the pending sends and, once none remain,
    flips it to SENT. DRAFT is the only status start_campaign accepts, so a
    double-trigger can't fan out a second batch (the trigger re-checks under a
    transaction -- see start_campaign).

    THE SEGMENT is a (kind, params) pair, not a stored recipient list: v1
    supports three kinds (all opted-in / bought a specific event / minimum
    lifetime spend), each reading the params relevant to it (segment_event,
    segment_min_spend). segment_guests in services is the SINGLE place that
    turns this into a GuestAccount queryset, so the composer's live preview
    count and the trigger's actual fan-out are guaranteed to agree. Every kind
    is further filtered to opted-in guests with a non-blank email -- consent is
    never bypassed by a segment choice.

    body is PLAIN TEXT (paragraphs separated by blank lines); the HTML email is
    rendered from it with linebreaks (campaigns.emails.render_campaign), so
    staff compose once and both MIME parts stay in sync. recipient_count is the
    snapshot taken at trigger for the dashboard's sent/queued display -- it is
    the count of CampaignSend rows created, NOT a live segment re-count."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        CANCELLED = "cancelled", "Cancelled"

    class SegmentKind(models.TextChoices):
        ALL = "all", "All opted-in"
        EVENT = "event", "Bought a specific event"
        MIN_SPEND = "min_spend", "Minimum lifetime spend"

    name = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    # Plain-text body, paragraphs separated by blank lines. render_campaign
    # turns this into the text part verbatim and the HTML part via `linebreaks`,
    # so there's one authored source for both MIME alternatives.
    body = models.TextField()

    segment_kind = models.CharField(
        max_length=20, choices=SegmentKind.choices, default=SegmentKind.ALL
    )
    # EVENT segment param: guests who bought (a PAID order for) this event. SET_NULL
    # so retiring an event doesn't cascade-delete the campaigns that once targeted
    # it -- the historical send log is worth keeping even after the event is gone.
    segment_event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_campaigns",
    )
    # MIN_SPEND segment param: guests whose lifetime PAID total is >= this.
    segment_min_spend = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    # Which staff User composed/triggered this. SET_NULL so a departed staffer's
    # deletion doesn't take the campaign history with them.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_campaigns",
    )
    # Recipient count SNAPSHOTTED at trigger (== number of CampaignSend rows
    # created by start_campaign), not a live segment re-count -- so the
    # dashboard's "sent to N" stays fixed even as guests later opt in/out.
    recipient_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Stamped when the LAST pending send drains (the batch sender flips the
    # campaign to SENT). Null until then.
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            # The dashboard lists a tenant's campaigns filtered by lifecycle
            # (drafts to finish, what's currently sending); (org, status) covers it.
            models.Index(fields=["organization", "status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"


class CampaignSend(TenantScopedModel):
    """One recipient's delivery record for a campaign -- the durable work queue
    the cron batch sender drains, and the audit log of what happened per address.

    Created in bulk by campaigns.services.start_campaign (one row per segmented
    guest) with status=PENDING. The batch sender
    (campaigns.management.commands.send_campaign_emails) claims each PENDING row
    atomically, re-checks the guest's opt-in at send time, sends, and records the
    outcome (SENT / FAILED with an error message / SKIPPED if consent was
    withdrawn between trigger and send). email is SNAPSHOTTED at trigger: it's
    the address the campaign was queued to, preserved even if the guest later
    changes it, so the log reflects where mail actually went.

    THE UNIQUE (campaign, guest) CONSTRAINT is what makes re-triggering /
    concurrent triggers safe: start_campaign bulk-creates with
    ignore_conflicts=True, so a guest already queued for this campaign is never
    double-queued (and thus never double-emailed). One send per person per
    campaign is the backstop the DB enforces, not just app logic."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    campaign = models.ForeignKey(
        EmailCampaign, on_delete=models.CASCADE, related_name="sends"
    )
    guest = models.ForeignKey(
        "guests.GuestAccount", on_delete=models.CASCADE, related_name="campaign_sends"
    )
    # The recipient address as of trigger time -- snapshotted so the log records
    # where the mail was actually sent even if the guest later edits their email.
    email = models.EmailField()

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    # Populated on FAILED with the exception text (truncated by the sender). Blank
    # otherwise.
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        constraints = [
            # One send row per (campaign, guest): the backstop that makes
            # bulk_create(ignore_conflicts=True) at trigger idempotent, so a
            # re-trigger can never double-email a recipient.
            models.UniqueConstraint(
                fields=["campaign", "guest"],
                name="unique_send_per_campaign_guest",
            ),
        ]
        indexes = TenantScopedModel.Meta.indexes + [
            # The sender's hot query is "pending sends for a sending campaign"
            # (org-scoped); the campaign summary reads sends by (campaign,
            # status). Both covered here.
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["campaign", "status"]),
        ]

    def __str__(self):
        return f"{self.email} <- {self.campaign_id} ({self.get_status_display()})"
