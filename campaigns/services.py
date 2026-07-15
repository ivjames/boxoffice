"""Campaign service layer: segment materialization, the send trigger, and the
dashboard audience query.

DEPENDENCY DIRECTION (mirrors donations/passes -- keep this app off the money
path): this module imports ONLY from guests and orders/events models. It must
NOT import from dashboard or any views -- the dashboard depends on campaigns,
never the reverse -- so the segment/trigger logic stays independently testable
and reusable (the cron sender and the composer preview both call it).

THE SINGLE-MATERIALIZATION RULE: segment_guests is the one query that turns an
EmailCampaign's (kind, params) into a concrete GuestAccount set. The composer's
live preview count (segment_recipient_count) and the trigger's actual fan-out
(start_campaign) BOTH go through it, so "we'll send to N people" and "we created
N sends" can never disagree.
"""

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from guests.models import GuestAccount
from orders.models import Order

from .models import CampaignSend, EmailCampaign


class CampaignStateError(Exception):
    """Raised by start_campaign when a campaign isn't in a triggerable state
    (not DRAFT -- already sending, sent, or cancelled). The message is safe to
    surface to staff; it means "this campaign can't be sent right now", not a
    bug."""


def segment_guests(campaign):
    """THE segment query: the GuestAccount queryset a campaign will send to.

    Used by BOTH the composer preview (via segment_recipient_count) and the
    trigger (start_campaign), so a previewed count and the sends actually
    created are the same set by construction -- this is the single place a
    segment is materialized.

    Every kind starts from the SAME consent-gated base: this org's guests who
    are opted in AND have a non-blank email (a blank-email guest -- possible
    from a Stripe session that carried none -- can't be mailed, so it's excluded
    everywhere). On top of that base:

      ALL       -- the base as-is: every opted-in, mailable guest.
      EVENT     -- base further filtered to guests with a PAID order for
                   segment_event. A guest with NO orders is therefore excluded
                   (they never bought this event); .distinct() collapses the
                   join fan-out so a guest with several tickets to the event
                   still counts once.
      MIN_SPEND -- base annotated with lifetime PAID spend (Sum of Order.total
                   over PAID orders) and filtered to spend >= segment_min_spend.
                   A guest with no PAID orders annotates to NULL, which fails the
                   >= filter, so no-order guests are excluded here too.

    So no-order guests appear ONLY in the ALL segment (they opted in but haven't
    bought), never in EVENT/MIN_SPEND -- the intended semantics of "bought X" /
    "spent at least Y". Uses the Order.Status.PAID enum throughout so this
    tracks whatever "paid" means canonically."""
    base = (
        GuestAccount.objects.for_organization(campaign.organization)
        .filter(marketing_opt_in=True)
        .exclude(email="")
    )

    kind = campaign.segment_kind
    if kind == EmailCampaign.SegmentKind.EVENT:
        return base.filter(
            orders__status=Order.Status.PAID,
            orders__performance__event=campaign.segment_event,
        ).distinct()
    if kind == EmailCampaign.SegmentKind.MIN_SPEND:
        return base.annotate(
            spend=Sum("orders__total", filter=Q(orders__status=Order.Status.PAID))
        ).filter(spend__gte=campaign.segment_min_spend)
    # ALL (and the default): every opted-in, mailable guest.
    return base


def segment_recipient_count(campaign):
    """How many recipients `campaign` would send to right now -- the composer's
    live preview number. A thin .count() over segment_guests so preview and
    fan-out share the exact same query (see segment_guests)."""
    return segment_guests(campaign).count()


@transaction.atomic
def start_campaign(campaign):
    """Trigger `campaign`: materialize its segment into PENDING CampaignSend
    rows, snapshot the recipient count, and flip it DRAFT -> SENDING. Returns
    the number of sends created (== recipient_count).

    Re-checks status == DRAFT UNDER this transaction and raises
    CampaignStateError otherwise, so a double-click / concurrent trigger can't
    fan out a second batch: only the first caller sees DRAFT; the second finds
    SENDING and is rejected. (Belt-and-suspenders with the CampaignSend
    unique(campaign, guest) constraint below, which independently prevents
    double-queuing a recipient even if two triggers did race in.)

    bulk_create with ignore_conflicts=True so re-materializing the same segment
    (or a concurrent trigger that slipped past the status check) silently skips
    any (campaign, guest) already queued rather than erroring -- one send per
    recipient per campaign. email is snapshotted from guest.email at THIS moment
    (the address the campaign was queued to). recipient_count is set to the
    number of rows we intended to create (the segment size), which is the
    dashboard's "queued to N" figure.

    The actual sending is NOT done here -- that's the cron batch sender's job
    (send_campaign_emails), which is why this only creates PENDING rows and
    leaves the campaign SENDING. Decoupling trigger from send keeps the trigger
    a fast, transactional bookkeeping step and lets delivery be retried/paced
    without re-segmenting."""
    # Re-read the status under the transaction so the DRAFT check and the flip
    # to SENDING are atomic against a concurrent trigger.
    locked = EmailCampaign.objects.select_for_update().get(pk=campaign.pk)
    if locked.status != EmailCampaign.Status.DRAFT:
        raise CampaignStateError(
            f"Campaign {locked.pk} is {locked.get_status_display()}, not a draft; "
            "it can't be sent again."
        )

    guests = list(segment_guests(locked))
    sends = [
        CampaignSend(
            organization=locked.organization,
            campaign=locked,
            guest=guest,
            email=guest.email,
            status=CampaignSend.Status.PENDING,
        )
        for guest in guests
    ]
    CampaignSend.objects.bulk_create(sends, ignore_conflicts=True)

    count = len(guests)
    locked.recipient_count = count
    if count == 0:
        # An empty segment (a brand-new tenant, or an event/min-spend filter no
        # opted-in guest matches) creates zero PENDING rows. The cron sender only
        # flips a campaign to SENT after draining sends it actually touched, so a
        # zero-send campaign left SENDING would hang there forever -- never SENT,
        # never re-editable (the composer only edits drafts). Finish it here:
        # there is nothing to send, so it's already sent.
        locked.status = EmailCampaign.Status.SENT
        locked.sent_at = timezone.now()
        locked.save(update_fields=["recipient_count", "status", "sent_at", "updated_at"])
    else:
        locked.status = EmailCampaign.Status.SENDING
        locked.save(update_fields=["recipient_count", "status", "updated_at"])
    return count


def audience_queryset(organization, *, search="", opt_in=None, tag=""):
    """The dashboard's audience list: `organization`'s guests annotated with the
    computed CRM metrics (order_count, ltv) and filtered by the staff controls.

    order_count / ltv are COMPUTED here off PAID orders (Count / Sum with a
    PAID filter), never stored on GuestAccount -- so they can't drift from the
    orders they summarize (see the field comment in guests.models). A guest with
    no PAID orders annotates to order_count=0, ltv=NULL.

    Filters (all optional, AND-combined):
      search  -- case-insensitive match on email OR name.
      opt_in  -- when not None, filter to guests whose marketing_opt_in matches
                 the bool (None = don't filter, show everyone).
      tag     -- case-insensitive substring match on the tags CSV (a coarse
                 "has this label" filter; exact per-tag matching is the
                 tag_list caller's job).

    Ordered by lifetime value descending (biggest spenders first -- the natural
    audience-triage order), with email as a stable tiebreaker so no-spend guests
    (ltv NULL) still sort deterministically."""
    qs = GuestAccount.objects.for_organization(organization).annotate(
        order_count=Count(
            "orders", filter=Q(orders__status=Order.Status.PAID), distinct=True
        ),
        ltv=Sum("orders__total", filter=Q(orders__status=Order.Status.PAID)),
    )
    if search:
        qs = qs.filter(Q(email__icontains=search) | Q(name__icontains=search))
    if opt_in is not None:
        qs = qs.filter(marketing_opt_in=opt_in)
    if tag:
        qs = qs.filter(tags__icontains=tag)
    # -ltv with NULLs (no-spend guests) sorted last on every backend (plain
    # "-ltv" leaves NULL ordering DB-dependent); email is the stable tiebreaker.
    return qs.order_by(F("ltv").desc(nulls_last=True), "email")
