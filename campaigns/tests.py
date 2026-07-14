"""Service-layer + command tests for the Phase 4 campaigns app (CRM / email
marketing).

Scope of THIS file (the service/model slice -- views/urls/forms/templates and
the dashboard tests are owned elsewhere):

  * segment_guests / segment_recipient_count -- the single segment
    materialization query (ALL / EVENT / MIN_SPEND), consent-gated and
    tenant-scoped, with the preview==fan-out invariant.
  * start_campaign -- the trigger that fans a DRAFT campaign out into PENDING
    CampaignSend rows, snapshots recipient_count, flips DRAFT->SENDING, and is
    idempotent under a re-trigger (unique (campaign, guest) + ignore_conflicts).
  * render_campaign / send_campaign_send -- the render + one-email send, driven
    directly with an explicit unsubscribe_url so they need no URL resolution.
  * send_campaign_emails -- the cron batch sender command.
  * audience_queryset -- the dashboard audience query (order_count / ltv
    annotations, search / opt-in / tag filters), a campaigns.services fn tested
    here alongside its module.

TWO DELIBERATE DEFERRALS to the UI-agent's slice (see the notes on the classes):

  * The campaign email TEMPLATES (templates/campaigns/email/campaign.{txt,html})
    are authored by the UI layer and are NOT present in this slice. The
    render/send/command tests inject minimal in-memory templates via
    @override_settings(TEMPLATES=...) -- a test fixture that keeps this slice
    self-contained and asserts the SEND PIPELINE (body carries the unsubscribe
    link, List-Unsubscribe headers, one mail per recipient) independently of the
    UI's template design.
  * The one-click unsubscribe route reverse("guest_unsubscribe") is UI-owned and
    unresolved in this slice. The command (send_campaign_emails) mints its
    unsubscribe link via that route, so the command-level tests are gated behind
    @skipUnless(_guest_unsubscribe_available()): they are WRITTEN and will run
    automatically once the UI route lands (the orchestrator's full-suite run
    after integration), and are skipped -- not failed -- until then.
"""

from decimal import Decimal
from io import StringIO
from unittest import skipUnless

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier
from guests import services as guest_services
from guests.models import GuestAccount
from orders.models import Order
from venues.models import Venue
from venues.tests import make_org

from campaigns import services
from campaigns.emails import render_campaign, send_campaign_send, send_test_campaign_email
from guests.tokens import make_unsubscribe_token
from campaigns.models import CampaignSend, EmailCampaign
from campaigns.services import CampaignStateError


# --- Test fixtures for the UI-owned pieces this slice doesn't ship ------------

# Minimal stand-ins for the UI-authored campaign email templates. They exercise
# the FROZEN context contract render_campaign passes (campaign, body,
# unsubscribe_url) so the send-pipeline assertions (unsubscribe link in the
# body, headers) are meaningful without depending on the real templates' design.
CAMPAIGN_TEST_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {
            "loaders": [
                (
                    "django.template.loaders.locmem.Loader",
                    {
                        "campaigns/email/campaign.txt": (
                            "{{ campaign.subject }}\n\n{{ body }}\n\n"
                            "Unsubscribe: {{ unsubscribe_url }}\n"
                        ),
                        # body is pre-rendered HTML (linebreaks applied by
                        # render_campaign), so the real UI template drops it in
                        # as safe -- mirror that here so <p> survives.
                        "campaigns/email/campaign.html": (
                            "<h1>{{ campaign.subject }}</h1>"
                            "<div>{{ body|safe }}</div>"
                            '<a href="{{ unsubscribe_url }}">Unsubscribe</a>'
                        ),
                    },
                ),
            ],
        },
    },
]


def _guest_unsubscribe_available():
    """Whether the UI-owned reverse('guest_unsubscribe') route resolves yet.
    Gates the command-level send tests (the command mints its unsubscribe link
    through that route). Evaluated at import/decoration time -- False in this
    isolated service slice, True once the UI urls land."""
    try:
        reverse("guest_unsubscribe")
    except NoReverseMatch:
        return False
    return True


GUEST_UNSUB = _guest_unsubscribe_available()


class CampaignFixtureMixin:
    """Builds one org with two GA events/performances and a small guest roster
    to segment over. Everything the segment/trigger/audience tests need."""

    def build_world(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event_a = Event.objects.create(organization=self.org, title="Show A", slug="a")
        self.event_b = Event.objects.create(organization=self.org, title="Show B", slug="b")
        self.perf_a = self._performance(self.event_a)
        self.perf_b = self._performance(self.event_b)

    def _performance(self, event):
        perf = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org, performance=perf, capacity=100)
        PriceTier.objects.create(
            organization=self.org, performance=perf, name="GA", amount=Decimal("35.00")
        )
        return perf

    def guest(self, email, *, org=None, opt_in=True, name=""):
        return GuestAccount.objects.create(
            organization=org or self.org, email=email, name=name, marketing_opt_in=opt_in
        )

    def order(self, guest, *, total="35.00", performance=None, status=Order.Status.PAID, org=None):
        return Order.objects.create(
            organization=org or self.org,
            guest=guest,
            performance=performance,
            buyer_email=guest.email,
            total=Decimal(total),
            status=status,
        )

    def campaign(self, *, kind=EmailCampaign.SegmentKind.ALL, event=None, min_spend=None, org=None):
        return EmailCampaign.objects.create(
            organization=org or self.org,
            name="Spring news",
            subject="Spring at the Roxy",
            body="Line one.\n\nLine two.",
            segment_kind=kind,
            segment_event=event,
            segment_min_spend=(Decimal(min_spend) if min_spend is not None else None),
        )


# --- segment_guests -----------------------------------------------------------


class SegmentAllTests(CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.build_world()

    def test_all_returns_only_opted_in_mailable_guests(self):
        opted = self.guest("opted@example.com", opt_in=True)
        self.guest("out@example.com", opt_in=False)
        self.guest("", opt_in=True)  # opted in but no email -> can't be mailed

        result = list(services.segment_guests(self.campaign(kind=EmailCampaign.SegmentKind.ALL)))

        self.assertEqual(result, [opted])

    def test_all_includes_no_order_opted_in_guest(self):
        # No purchase at all, but opted in -> ALL still includes them (unlike
        # EVENT / MIN_SPEND).
        g = self.guest("newsletter@example.com", opt_in=True)
        result = list(services.segment_guests(self.campaign(kind=EmailCampaign.SegmentKind.ALL)))
        self.assertIn(g, result)


class SegmentEventTests(CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.build_world()

    def test_event_returns_distinct_paid_buyers_of_that_event_only(self):
        buyer = self.guest("buyer@example.com")
        # Two PAID orders for event A -> must collapse to ONE row (distinct).
        self.order(buyer, performance=self.perf_a)
        self.order(buyer, performance=self.perf_a)

        # A buyer of a DIFFERENT event -> excluded.
        self.order(self.guest("other@example.com"), performance=self.perf_b)
        # A non-PAID order for event A -> excluded.
        self.order(
            self.guest("pending@example.com"), performance=self.perf_a, status=Order.Status.PENDING
        )
        # An opted-in guest with no orders -> excluded (never bought this event).
        self.guest("noorders@example.com")

        result = list(
            services.segment_guests(
                self.campaign(kind=EmailCampaign.SegmentKind.EVENT, event=self.event_a)
            )
        )

        self.assertEqual(result, [buyer])

    def test_event_excludes_opted_out_paid_buyer(self):
        opted_out = self.guest("out@example.com", opt_in=False)
        self.order(opted_out, performance=self.perf_a)
        result = list(
            services.segment_guests(
                self.campaign(kind=EmailCampaign.SegmentKind.EVENT, event=self.event_a)
            )
        )
        self.assertEqual(result, [])


class SegmentMinSpendTests(CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.build_world()

    def test_min_spend_sums_paid_orders_and_boundary_is_inclusive(self):
        # Exactly at the threshold (50) via two PAID orders summing to 50 -> IN.
        at = self.guest("at@example.com")
        self.order(at, total="20.00", performance=self.perf_a)
        self.order(at, total="30.00", performance=self.perf_a)

        # Above -> IN.
        above = self.guest("above@example.com")
        self.order(above, total="80.00", performance=self.perf_a)

        # Below -> OUT.
        below = self.guest("below@example.com")
        self.order(below, total="49.99", performance=self.perf_a)

        # A non-PAID order doesn't count toward spend -> OUT.
        pending = self.guest("pending@example.com")
        self.order(pending, total="500.00", performance=self.perf_a, status=Order.Status.CANCELLED)

        # No orders -> spend NULL -> OUT.
        self.guest("noorders@example.com")

        result = set(
            services.segment_guests(
                self.campaign(kind=EmailCampaign.SegmentKind.MIN_SPEND, min_spend="50")
            )
        )

        self.assertEqual(result, {at, above})


class SegmentCrossTenantAndInvariantTests(CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.build_world()

    def test_campaign_never_returns_another_orgs_guests_same_email(self):
        org_b = make_org("otherhouse")
        # Same email address, opted in, in BOTH orgs.
        mine = self.guest("shared@example.com", org=self.org, opt_in=True)
        self.guest("shared@example.com", org=org_b, opt_in=True)

        result = list(services.segment_guests(self.campaign(kind=EmailCampaign.SegmentKind.ALL)))

        self.assertEqual(result, [mine])

    def test_recipient_count_equals_materialized_count(self):
        self.guest("a@example.com")
        self.guest("b@example.com")
        self.guest("out@example.com", opt_in=False)
        campaign = self.campaign(kind=EmailCampaign.SegmentKind.ALL)

        self.assertEqual(
            services.segment_recipient_count(campaign),
            services.segment_guests(campaign).count(),
        )
        self.assertEqual(services.segment_recipient_count(campaign), 2)


# --- start_campaign -----------------------------------------------------------


class StartCampaignTests(CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.build_world()
        self.g1 = self.guest("one@example.com")
        self.g2 = self.guest("two@example.com")
        self.guest("out@example.com", opt_in=False)  # never a recipient

    def test_trigger_creates_one_pending_send_per_recipient_and_flips_status(self):
        campaign = self.campaign(kind=EmailCampaign.SegmentKind.ALL)

        count = services.start_campaign(campaign)

        self.assertEqual(count, 2)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, EmailCampaign.Status.SENDING)
        self.assertEqual(campaign.recipient_count, 2)

        sends = campaign.sends.all()
        self.assertEqual(sends.count(), 2)
        for send in sends:
            self.assertEqual(send.status, CampaignSend.Status.PENDING)
            self.assertEqual(send.organization_id, self.org.pk)
        # email is snapshotted from each guest.
        self.assertEqual(
            {s.email for s in sends}, {"one@example.com", "two@example.com"}
        )
        self.assertEqual({s.guest_id for s in sends}, {self.g1.pk, self.g2.pk})

    def test_empty_segment_campaign_finishes_immediately_as_sent(self):
        """A segment nobody matches creates zero PENDING sends. The cron sender
        only flips campaigns it touches, so a zero-send campaign left SENDING
        would hang there forever -- never SENT, never re-editable. start_campaign
        must finish it on the spot instead."""
        # A min-spend threshold none of the opted-in guests meet -> empty segment.
        campaign = self.campaign(kind=EmailCampaign.SegmentKind.MIN_SPEND, min_spend="500.00")

        count = services.start_campaign(campaign)

        self.assertEqual(count, 0)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, EmailCampaign.Status.SENT)
        self.assertIsNotNone(campaign.sent_at)
        self.assertEqual(campaign.recipient_count, 0)
        self.assertEqual(campaign.sends.count(), 0)

    def test_second_trigger_of_a_sending_campaign_raises_state_error(self):
        campaign = self.campaign(kind=EmailCampaign.SegmentKind.ALL)
        services.start_campaign(campaign)

        with self.assertRaises(CampaignStateError):
            services.start_campaign(campaign)

    def test_retrigger_is_idempotent_no_duplicate_send_rows(self):
        """The unique (campaign, guest) constraint + ignore_conflicts means
        re-materializing the SAME segment never double-queues a recipient. We
        reset the guard status to DRAFT to reach the bulk_create a second time
        and prove the DB-level dedupe (not just the status guard) holds."""
        campaign = self.campaign(kind=EmailCampaign.SegmentKind.ALL)
        services.start_campaign(campaign)
        self.assertEqual(CampaignSend.objects.filter(campaign=campaign).count(), 2)

        # Force it back to DRAFT so start_campaign runs the fan-out again.
        campaign.status = EmailCampaign.Status.DRAFT
        campaign.save(update_fields=["status"])
        services.start_campaign(campaign)

        # Still exactly two rows -- the re-created (campaign, guest) pairs were
        # silently ignored, not duplicated.
        self.assertEqual(CampaignSend.objects.filter(campaign=campaign).count(), 2)


# --- render_campaign / send_campaign_send (direct, explicit unsubscribe_url) --


@override_settings(TEMPLATES=CAMPAIGN_TEST_TEMPLATES)
class RenderAndSendCampaignTests(CampaignFixtureMixin, TestCase):
    """Drive the render + one-email send directly with an explicit
    unsubscribe_url, so no UI route resolution is needed. Uses the in-memory
    templates above (the real ones are UI-owned; see the module docstring)."""

    def setUp(self):
        self.build_world()
        self.g = self.guest("reader@example.com", name="Reader")
        self.campaign_obj = self.campaign(kind=EmailCampaign.SegmentKind.ALL)

    def test_render_campaign_returns_subject_and_both_bodies(self):
        subject, text_body, html_body = render_campaign(
            self.campaign_obj, self.g, unsubscribe_url="https://roxy.example/unsub?token=xyz"
        )
        self.assertEqual(subject, self.campaign_obj.subject)
        self.assertIn("https://roxy.example/unsub?token=xyz", text_body)
        self.assertIn("https://roxy.example/unsub?token=xyz", html_body)
        # Plain body flows into the text part; HTML part gets <p>-wrapped body.
        self.assertIn("Line one.", text_body)
        self.assertIn("<p>", html_body)

    def test_send_campaign_send_emails_the_snapshot_address_with_unsub_link(self):
        send = CampaignSend.objects.create(
            organization=self.org,
            campaign=self.campaign_obj,
            guest=self.g,
            email="snapshot@example.com",  # snapshot, not a live guest.email read
            status=CampaignSend.Status.PENDING,
        )
        url = "https://roxy.example/unsubscribe/?token=abc"

        send_campaign_send(send, unsubscribe_url=url)

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["snapshot@example.com"])
        self.assertEqual(msg.subject, self.campaign_obj.subject)
        self.assertIn(url, msg.body)  # in-body unsubscribe link
        self.assertEqual(msg.extra_headers["List-Unsubscribe"], f"<{url}>")
        self.assertEqual(
            msg.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click"
        )
        # An HTML alternative is attached alongside the text body.
        self.assertTrue(any(ctype == "text/html" for _, ctype in msg.alternatives))

    def test_test_send_uses_placeholder_link_not_a_real_guests_token(self):
        """A test send goes to the staffer, but its footer link + List-
        Unsubscribe header must NOT carry a live one-click token for whichever
        real subscriber seeded the preview -- else the staffer's click (or a
        mail scanner fetching links) silently opts out a customer. The URL must
        be the bare, tokenless unsubscribe page (which opts out nobody)."""
        # A real opted-in guest exists (self.g) and would seed the preview.
        real_token = make_unsubscribe_token(self.g)

        send_test_campaign_email(self.campaign_obj, "staffer@theater.test")

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["staffer@theater.test"])
        # No live token anywhere in the message (body or List-Unsubscribe header).
        self.assertNotIn(real_token, msg.body)
        self.assertNotIn("token=", msg.body)
        self.assertNotIn(real_token, msg.extra_headers.get("List-Unsubscribe", ""))
        self.assertNotIn("token=", msg.extra_headers.get("List-Unsubscribe", ""))
        # And the real subscriber is untouched.
        self.g.refresh_from_db()
        self.assertTrue(self.g.marketing_opt_in)

    @skipUnless(GUEST_UNSUB, "needs UI-owned reverse('guest_unsubscribe')")
    def test_send_builds_its_own_unsub_link_when_none_passed(self):
        send = CampaignSend.objects.create(
            organization=self.org,
            campaign=self.campaign_obj,
            guest=self.g,
            email=self.g.email,
            status=CampaignSend.Status.PENDING,
        )
        send_campaign_send(send)  # no unsubscribe_url -> built off the guest
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("List-Unsubscribe", mail.outbox[0].extra_headers)


# --- send_campaign_emails command --------------------------------------------


@skipUnless(GUEST_UNSUB, "command mints reverse('guest_unsubscribe') -- UI-owned route")
@override_settings(
    TEMPLATES=CAMPAIGN_TEST_TEMPLATES,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class SendCampaignEmailsCommandTests(CampaignFixtureMixin, TestCase):
    """The cron batch sender. Deferred behind @skipUnless until the UI's
    guest_unsubscribe route lands (the command reverses it per recipient); the
    orchestrator's post-integration full-suite run exercises these. Uses the
    in-memory templates so the assertions don't depend on the UI templates."""

    def setUp(self):
        self.build_world()
        self.g1 = self.guest("one@example.com")
        self.g2 = self.guest("two@example.com")
        self.campaign_obj = self.campaign(kind=EmailCampaign.SegmentKind.ALL)

    def _run(self):
        call_command("send_campaign_emails", stdout=StringIO())

    def test_drains_pending_marks_sent_and_flips_campaign_sent(self):
        services.start_campaign(self.campaign_obj)

        self._run()

        sends = self.campaign_obj.sends.all()
        self.assertTrue(all(s.status == CampaignSend.Status.SENT for s in sends))
        self.assertTrue(all(s.sent_at is not None for s in sends))
        self.assertEqual(len(mail.outbox), 2)
        for msg in mail.outbox:
            self.assertIn("List-Unsubscribe", msg.extra_headers)
            self.assertIn("/unsubscribe", msg.body.lower() + msg.extra_headers["List-Unsubscribe"].lower())
        self.campaign_obj.refresh_from_db()
        self.assertEqual(self.campaign_obj.status, EmailCampaign.Status.SENT)
        self.assertIsNotNone(self.campaign_obj.sent_at)

    @override_settings(CAMPAIGN_BATCH_SIZE=1)
    def test_batch_cap_paces_across_ticks(self):
        services.start_campaign(self.campaign_obj)

        self._run()  # one tick: sends 1, leaves 1 pending
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            self.campaign_obj.sends.filter(status=CampaignSend.Status.PENDING).count(), 1
        )
        self.campaign_obj.refresh_from_db()
        self.assertEqual(self.campaign_obj.status, EmailCampaign.Status.SENDING)

        self._run()  # second tick: finishes
        self.assertEqual(len(mail.outbox), 2)
        self.campaign_obj.refresh_from_db()
        self.assertEqual(self.campaign_obj.status, EmailCampaign.Status.SENT)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST=""
    )
    def test_no_op_when_email_delivery_not_configured(self):
        services.start_campaign(self.campaign_obj)

        self._run()

        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(
            all(s.status == CampaignSend.Status.PENDING for s in self.campaign_obj.sends.all())
        )
        self.campaign_obj.refresh_from_db()
        self.assertEqual(self.campaign_obj.status, EmailCampaign.Status.SENDING)

    def test_opt_out_between_trigger_and_send_is_skipped(self):
        services.start_campaign(self.campaign_obj)
        # g1 unsubscribes after the campaign was queued but before this tick.
        guest_services.set_marketing_opt_in(self.g1, False)

        self._run()

        g1_send = self.campaign_obj.sends.get(guest=self.g1)
        g2_send = self.campaign_obj.sends.get(guest=self.g2)
        self.assertEqual(g1_send.status, CampaignSend.Status.SKIPPED)
        self.assertEqual(g2_send.status, CampaignSend.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)  # only g2 got mail
        self.assertEqual(mail.outbox[0].to, [self.g2.email])
        self.campaign_obj.refresh_from_db()
        self.assertEqual(self.campaign_obj.status, EmailCampaign.Status.SENT)

    def test_a_failing_send_is_recorded_failed_and_others_still_sent(self):
        services.start_campaign(self.campaign_obj)
        import campaigns.emails as emails_mod
        from unittest.mock import patch

        real_send = emails_mod.send_campaign_send

        def flaky(send, **kwargs):
            if send.email == self.g1.email:
                raise RuntimeError("smtp exploded")
            return real_send(send, **kwargs)

        with patch(
            "campaigns.management.commands.send_campaign_emails.send_campaign_send",
            side_effect=flaky,
        ):
            self._run()

        g1_send = self.campaign_obj.sends.get(guest=self.g1)
        g2_send = self.campaign_obj.sends.get(guest=self.g2)
        self.assertEqual(g1_send.status, CampaignSend.Status.FAILED)
        self.assertIn("smtp exploded", g1_send.error)
        self.assertEqual(g2_send.status, CampaignSend.Status.SENT)
        # No pending/sending left -> campaign still flips SENT despite the failure.
        self.campaign_obj.refresh_from_db()
        self.assertEqual(self.campaign_obj.status, EmailCampaign.Status.SENT)

    def test_a_row_already_claimed_sending_is_not_reclaimed(self):
        services.start_campaign(self.campaign_obj)
        # Simulate a concurrent tick having claimed g1's row already.
        claimed = self.campaign_obj.sends.get(guest=self.g1)
        claimed.status = CampaignSend.Status.SENDING
        claimed.save(update_fields=["status"])

        self._run()

        claimed.refresh_from_db()
        # The command's pending query filters status=PENDING, so the pre-claimed
        # row is never re-claimed / re-sent (no double count).
        self.assertEqual(claimed.status, CampaignSend.Status.SENDING)
        self.assertIsNone(claimed.sent_at)
        self.assertEqual(len(mail.outbox), 1)  # only the genuinely-pending g2 row
        self.assertEqual(
            self.campaign_obj.sends.get(guest=self.g2).status, CampaignSend.Status.SENT
        )


# --- audience_queryset (dashboard audience service fn) ------------------------


class AudienceQuerysetTests(CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.build_world()

    def test_order_count_and_ltv_count_only_paid_orders(self):
        g = self.guest("spender@example.com")
        self.order(g, total="35.00", performance=self.perf_a)
        self.order(g, total="65.00", performance=self.perf_a)
        # A non-PAID order must not count toward order_count or ltv.
        self.order(g, total="999.00", performance=self.perf_a, status=Order.Status.PENDING)

        row = services.audience_queryset(self.org).get(pk=g.pk)
        self.assertEqual(row.order_count, 2)
        self.assertEqual(row.ltv, Decimal("100.00"))

    def test_no_paid_orders_annotates_zero_count_and_null_ltv(self):
        g = self.guest("browser@example.com")
        row = services.audience_queryset(self.org).get(pk=g.pk)
        self.assertEqual(row.order_count, 0)
        self.assertIsNone(row.ltv)

    def test_search_matches_email_or_name(self):
        self.guest("alice@example.com", name="Alice Adams")
        self.guest("bob@example.com", name="Bob Brown")

        by_email = services.audience_queryset(self.org, search="alice@")
        self.assertEqual([g.email for g in by_email], ["alice@example.com"])

        by_name = services.audience_queryset(self.org, search="brown")
        self.assertEqual([g.email for g in by_name], ["bob@example.com"])

    def test_opt_in_filter(self):
        opted = self.guest("in@example.com", opt_in=True)
        out = self.guest("out@example.com", opt_in=False)

        self.assertEqual(
            set(services.audience_queryset(self.org, opt_in=True).values_list("pk", flat=True)),
            {opted.pk},
        )
        self.assertEqual(
            set(services.audience_queryset(self.org, opt_in=False).values_list("pk", flat=True)),
            {out.pk},
        )
        # None -> no filter, everyone.
        self.assertEqual(services.audience_queryset(self.org).count(), 2)

    def test_tag_icontains_filter(self):
        vip = self.guest("vip@example.com")
        vip.tags = "vip,board"
        vip.save(update_fields=["tags"])
        self.guest("plain@example.com")

        result = services.audience_queryset(self.org, tag="VIP")  # case-insensitive
        self.assertEqual([g.email for g in result], ["vip@example.com"])

    def test_ordered_by_ltv_desc_with_null_spenders_last(self):
        big = self.guest("big@example.com")
        self.order(big, total="200.00", performance=self.perf_a)
        small = self.guest("small@example.com")
        self.order(small, total="10.00", performance=self.perf_a)
        self.guest("none@example.com")  # ltv NULL -> sorts last

        emails = [g.email for g in services.audience_queryset(self.org)]
        self.assertEqual(emails[0], "big@example.com")
        self.assertEqual(emails[1], "small@example.com")
        self.assertEqual(emails[-1], "none@example.com")
