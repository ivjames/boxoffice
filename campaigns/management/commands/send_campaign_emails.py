"""Batch-send queued campaign emails -- the CRON worker half of Phase 4's
email marketing, the exact same shape as the Hold sweeper and the tenant
provisioner (a management command run every minute or two by cron; no Celery,
per docs/ROADMAP.md).

campaigns.services.start_campaign fans a triggered campaign out into PENDING
CampaignSend rows and flips the campaign to SENDING; THIS command drains those
rows: it claims each one atomically (so overlapping cron ticks can't double-
send), re-checks the guest's opt-in at send time (consent may have been
withdrawn between trigger and now), sends the one email, and records the
outcome on the row. When a campaign's last pending/sending row drains it flips
the campaign to SENT.

Safe to run every minute: a no-op when nothing is PENDING, each row claimed
via a conditional UPDATE (PENDING -> SENDING) so only one tick owns it, and
capped at settings.CAMPAIGN_BATCH_SIZE rows per run so a huge blast paces out
across ticks rather than blocking one run on thousands of SMTP round-trips.

Must run under prod settings (DJANGO_SETTINGS_MODULE=config.settings.prod) so
it reads the same DB the dashboard writes triggers to -- see
deploy/boxoffice-campaigns.cron.
"""

from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from guests import services as guest_services
from guests.models import GuestAccount
from guests.tokens import make_unsubscribe_token

from campaigns.emails import send_campaign_send, tenant_base_url
from campaigns.models import CampaignSend, EmailCampaign


class Command(BaseCommand):
    help = (
        "Send queued campaign emails (CampaignSend rows in status=pending for a "
        "sending campaign). Run every minute via cron; see "
        "deploy/boxoffice-campaigns.cron."
    )

    def handle(self, *args, **options):
        from django.conf import settings

        # Gate on deliverability exactly like the guest portal does: if the prod
        # SMTP backend is selected but EMAIL_HOST is still blank, mail won't
        # actually leave the box -- don't burn CampaignSend rows against a dead
        # transport. Leave them PENDING so a real send happens once SMTP is wired.
        if not guest_services.email_delivery_configured():
            self.stdout.write(
                "Email delivery is not configured yet (SMTP backend with no "
                "EMAIL_HOST); leaving campaign sends queued."
            )
            return

        batch_size = settings.CAMPAIGN_BATCH_SIZE
        pending = list(
            CampaignSend.objects.filter(
                status=CampaignSend.Status.PENDING,
                campaign__status=EmailCampaign.Status.SENDING,
            )
            .select_related("campaign", "guest", "organization")
            .order_by("pk")[:batch_size]
        )
        if not pending:
            return  # nothing queued -- the common every-minute case

        sent = failed = skipped = 0
        touched_campaign_ids = set()

        for send in pending:
            touched_campaign_ids.add(send.campaign_id)

            # Claim it atomically: only the tick that flips PENDING->SENDING
            # (rowcount 1) proceeds, so two overlapping runs can't both send the
            # same row. Same pattern as provision_pending_tenants' row claim.
            claimed = CampaignSend.objects.filter(
                pk=send.pk, status=CampaignSend.Status.PENDING
            ).update(status=CampaignSend.Status.SENDING)
            if not claimed:
                continue

            # Re-check consent at SEND time, not just at trigger: a guest may
            # have unsubscribed (portal toggle / a previous campaign's one-click
            # link) between when this campaign was queued and now. Reload the
            # guest fresh so we see the current flag, and SKIP (don't send, don't
            # fail) if they've opted out -- honoring the withdrawal.
            guest = GuestAccount.objects.filter(pk=send.guest_id).first()
            if guest is None or not guest.marketing_opt_in:
                CampaignSend.objects.filter(pk=send.pk).update(
                    status=CampaignSend.Status.SKIPPED
                )
                skipped += 1
                continue

            # Mint the one-click unsubscribe link for THIS guest, absolute off
            # the tenant origin (no request under cron). reverse() resolves the
            # UI-layer route "guest_unsubscribe" -- which exists at run time
            # (added with the campaigns views); see the module docstring.
            unsubscribe_url = (
                tenant_base_url(send.organization)
                + reverse("guest_unsubscribe")
                + "?token="
                + make_unsubscribe_token(guest)
            )

            try:
                send_campaign_send(send, unsubscribe_url=unsubscribe_url)
            except Exception as exc:  # noqa: BLE001 -- record ANY send failure
                CampaignSend.objects.filter(pk=send.pk).update(
                    status=CampaignSend.Status.FAILED,
                    error=str(exc)[:2000],
                )
                failed += 1
                continue

            CampaignSend.objects.filter(pk=send.pk).update(
                status=CampaignSend.Status.SENT, sent_at=timezone.now()
            )
            sent += 1

        # A campaign is DONE once none of its sends are still PENDING/SENDING.
        # Check each campaign this batch touched and flip finished ones to SENT.
        # Guarded on status=SENDING so a cancelled campaign is never resurrected.
        now = timezone.now()
        for campaign_id in touched_campaign_ids:
            remaining = CampaignSend.objects.filter(
                campaign_id=campaign_id,
                status__in=[CampaignSend.Status.PENDING, CampaignSend.Status.SENDING],
            ).exists()
            if not remaining:
                EmailCampaign.objects.filter(
                    pk=campaign_id, status=EmailCampaign.Status.SENDING
                ).update(status=EmailCampaign.Status.SENT, sent_at=now)

        self.stdout.write(
            f"campaign sends: {sent} sent, {failed} failed, {skipped} skipped "
            f"(batch of {len(pending)})."
        )
