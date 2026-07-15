"""HTTP-level integration tests for the dashboard Email Campaigns area
(dashboard.views.EmailCampaign List/Create/Update + campaign_detail /
campaign_preview / campaign_test / campaign_send): manager+ role gating,
tenant isolation, the EmailCampaignForm's segment-param validation, the live
recipient-count preview, the "send me a test" mail, and the send trigger's
DRAFT -> SENDING state machine (idempotent under a double-send). Setup style
mirrors dashboard/test_passes.py / dashboard/test_donations.py; the locmem
email backend (Django's test default, pinned here) captures test mail exactly
as campaigns/tests.py does.
"""

from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from campaigns.models import CampaignSend, EmailCampaign
from dashboard.tests import DashFixtureMixin
from events.models import Event
from guests.models import GuestAccount


class CampaignFixtureMixin(DashFixtureMixin):
    def make_campaign(self, org, *, status=EmailCampaign.Status.DRAFT, created_by=None, **kwargs):
        defaults = dict(
            organization=org,
            name="Spring news",
            subject="Spring at the Roxy",
            body="Line one.\n\nLine two.",
            segment_kind=EmailCampaign.SegmentKind.ALL,
            status=status,
            created_by=created_by,
        )
        defaults.update(kwargs)
        return EmailCampaign.objects.create(**defaults)

    def opted_in_guest(self, org, email):
        return GuestAccount.objects.create(
            organization=org, email=email, marketing_opt_in=True
        )


class CampaignAccessTests(StaffFixtureMixin, CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.box_office = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.scanner = self.make_staff(self.org, Membership.Role.SCANNER)[0]

    def test_list_manager_and_above_only(self):
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/campaigns/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_create_get_manager_and_above_only(self):
        for user, expected in [(self.owner, 200), (self.manager, 200), (self.box_office, 403), (self.scanner, 403)]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get("/dashboard/campaigns/new/", HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_detail_and_edit_manager_and_above_only(self):
        campaign = self.make_campaign(self.org)
        for user, expected in [(self.owner, 200), (self.manager, 200), (self.box_office, 403), (self.scanner, 403)]:
            self.client.logout()
            self.client.force_login(user)
            detail = self.client.get(
                f"/dashboard/campaigns/{campaign.pk}/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(detail.status_code, expected, user.email)
            edit = self.client.get(
                f"/dashboard/campaigns/{campaign.pk}/edit/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(edit.status_code, expected, user.email)

    def test_send_manager_and_above_only(self):
        # manager_required is the outer decorator, so box office/scanner are
        # refused before require_POST even runs (403, not 405).
        campaign = self.make_campaign(self.org)
        for user, expected in [(self.box_office, 403), (self.scanner, 403), (self.manager, 302)]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.post(
                f"/dashboard/campaigns/{campaign.pk}/send/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, expected, user.email)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/dashboard/campaigns/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])


class CampaignCRUDTests(StaffFixtureMixin, CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def _create(self, **overrides):
        data = {
            "name": "Spring news",
            "subject": "Spring at the Roxy",
            "body": "Hello.\n\nCome see us.",
            "segment_kind": EmailCampaign.SegmentKind.ALL,
            "segment_event": "",
            "segment_min_spend": "",
        }
        data.update(overrides)
        return self.client.post("/dashboard/campaigns/new/", data, HTTP_HOST=host_for("roxy"))

    def test_create_all_segment_draft(self):
        resp = self._create()
        campaign = EmailCampaign.objects.get(organization=self.org)
        self.assertEqual(campaign.segment_kind, EmailCampaign.SegmentKind.ALL)
        self.assertEqual(campaign.status, EmailCampaign.Status.DRAFT)
        # created_by + organization are stamped by the view, never trusted from POST.
        self.assertEqual(campaign.created_by_id, self.manager.pk)
        self.assertEqual(campaign.organization_id, self.org.id)
        self.assertRedirects(
            resp, f"/dashboard/campaigns/{campaign.pk}/", fetch_redirect_response=False
        )

    def test_create_is_scoped_to_current_org_even_if_spoofed(self):
        self._create(organization=self.other_org.pk)
        campaign = EmailCampaign.objects.get(name="Spring news")
        self.assertEqual(campaign.organization_id, self.org.id)

    def test_event_segment_requires_event(self):
        resp = self._create(segment_kind=EmailCampaign.SegmentKind.EVENT, segment_event="")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Choose the event this campaign targets.")
        self.assertFalse(EmailCampaign.objects.filter(organization=self.org).exists())

    def test_event_segment_created_when_event_provided(self):
        event = Event.objects.create(organization=self.org, title="Show A", slug="a")
        resp = self._create(
            segment_kind=EmailCampaign.SegmentKind.EVENT, segment_event=event.pk
        )
        campaign = EmailCampaign.objects.get(organization=self.org)
        self.assertEqual(campaign.segment_kind, EmailCampaign.SegmentKind.EVENT)
        self.assertEqual(campaign.segment_event_id, event.pk)
        self.assertRedirects(
            resp, f"/dashboard/campaigns/{campaign.pk}/", fetch_redirect_response=False
        )

    def test_min_spend_segment_requires_amount(self):
        resp = self._create(
            segment_kind=EmailCampaign.SegmentKind.MIN_SPEND, segment_min_spend=""
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter a minimum lifetime spend amount.")
        self.assertFalse(EmailCampaign.objects.filter(organization=self.org).exists())

    def test_min_spend_segment_created_when_amount_provided(self):
        resp = self._create(
            segment_kind=EmailCampaign.SegmentKind.MIN_SPEND, segment_min_spend="50"
        )
        campaign = EmailCampaign.objects.get(organization=self.org)
        self.assertEqual(campaign.segment_kind, EmailCampaign.SegmentKind.MIN_SPEND)
        self.assertEqual(campaign.segment_min_spend, Decimal("50"))
        self.assertRedirects(
            resp, f"/dashboard/campaigns/{campaign.pk}/", fetch_redirect_response=False
        )

    def test_event_field_scoped_to_org(self):
        Event.objects.create(organization=self.org, title="Mine", slug="mine")
        Event.objects.create(organization=self.other_org, title="Theirs", slug="theirs")
        resp = self.client.get("/dashboard/campaigns/new/", HTTP_HOST=host_for("roxy"))
        titles = {e.title for e in resp.context["form"].fields["segment_event"].queryset}
        self.assertEqual(titles, {"Mine"})

    def test_update_edits_a_draft(self):
        campaign = self.make_campaign(self.org, created_by=self.manager)
        resp = self.client.post(
            f"/dashboard/campaigns/{campaign.pk}/edit/",
            {
                "name": "Renamed",
                "subject": "New subject",
                "body": "Rewritten body.",
                "segment_kind": EmailCampaign.SegmentKind.ALL,
                "segment_event": "",
                "segment_min_spend": "",
            },
            HTTP_HOST=host_for("roxy"),
        )
        self.assertRedirects(
            resp, f"/dashboard/campaigns/{campaign.pk}/", fetch_redirect_response=False
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.name, "Renamed")
        self.assertEqual(campaign.subject, "New subject")

    def test_update_of_non_draft_404s(self):
        # EmailCampaignUpdateView.get_queryset is DRAFT-only: a SENDING/SENT
        # campaign's content is fixed send history and can't be edited.
        campaign = self.make_campaign(self.org, status=EmailCampaign.Status.SENDING)
        resp = self.client.get(
            f"/dashboard/campaigns/{campaign.pk}/edit/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)

    def test_list_shows_only_this_orgs_campaigns(self):
        self.make_campaign(self.org, name="Mine")
        self.make_campaign(self.other_org, name="Theirs")
        resp = self.client.get("/dashboard/campaigns/", HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, "Mine")
        self.assertNotContains(resp, "Theirs")

    def test_update_cross_org_404s(self):
        other = self.make_campaign(self.other_org, name="Theirs")
        resp = self.client.get(
            f"/dashboard/campaigns/{other.pk}/edit/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)

    def test_detail_cross_org_404s(self):
        other = self.make_campaign(self.other_org, name="Theirs")
        resp = self.client.get(
            f"/dashboard/campaigns/{other.pk}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)


class CampaignPreviewTests(StaffFixtureMixin, CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_preview_returns_json_count_matching_segment(self):
        self.opted_in_guest(self.org, "a@example.com")
        self.opted_in_guest(self.org, "b@example.com")
        # An opted-out guest is never a recipient of an ALL segment.
        GuestAccount.objects.create(
            organization=self.org, email="out@example.com", marketing_opt_in=False
        )
        campaign = self.make_campaign(self.org, segment_kind=EmailCampaign.SegmentKind.ALL)

        resp = self.client.get(
            f"/dashboard/campaigns/{campaign.pk}/preview/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp.json(), {"count": 2})

    def test_preview_cross_org_404s(self):
        other = self.make_campaign(self.other_org)
        resp = self.client.get(
            f"/dashboard/campaigns/{other.pk}/preview/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CampaignTestEmailTests(StaffFixtureMixin, CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_test_send_emails_the_acting_staffer(self):
        # A real opted-in recipient so send_test renders against a live guest
        # (mints a genuine signed unsubscribe link), same as production.
        self.opted_in_guest(self.org, "reader@example.com")
        campaign = self.make_campaign(self.org)

        resp = self.client.post(
            f"/dashboard/campaigns/{campaign.pk}/test/", HTTP_HOST=host_for("roxy")
        )
        self.assertRedirects(
            resp, f"/dashboard/campaigns/{campaign.pk}/", fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.manager.email])
        # No CampaignSend rows are created by a test send.
        self.assertEqual(CampaignSend.objects.filter(campaign=campaign).count(), 0)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, EmailCampaign.Status.DRAFT)

    def test_test_send_works_with_no_segment_recipients(self):
        # No opted-in guests: send_test falls back to a synthetic sample guest;
        # the mail still goes to the staffer and nothing 500s.
        campaign = self.make_campaign(self.org)
        resp = self.client.post(
            f"/dashboard/campaigns/{campaign.pk}/test/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.manager.email])

    def test_test_send_cross_org_404s(self):
        other = self.make_campaign(self.other_org)
        resp = self.client.post(
            f"/dashboard/campaigns/{other.pk}/test/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(mail.outbox), 0)


class CampaignSendTests(StaffFixtureMixin, CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, self.other_venue = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_send_on_draft_materializes_pending_sends_and_flips_sending(self):
        self.opted_in_guest(self.org, "one@example.com")
        self.opted_in_guest(self.org, "two@example.com")
        campaign = self.make_campaign(self.org)

        resp = self.client.post(
            f"/dashboard/campaigns/{campaign.pk}/send/", HTTP_HOST=host_for("roxy")
        )
        self.assertRedirects(
            resp, f"/dashboard/campaigns/{campaign.pk}/", fetch_redirect_response=False
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, EmailCampaign.Status.SENDING)
        self.assertEqual(campaign.recipient_count, 2)

        sends = CampaignSend.objects.filter(campaign=campaign)
        self.assertEqual(sends.count(), 2)
        self.assertTrue(all(s.status == CampaignSend.Status.PENDING for s in sends))
        self.assertEqual(
            {s.email for s in sends}, {"one@example.com", "two@example.com"}
        )

    def test_second_send_flashes_state_error_and_does_not_duplicate_rows(self):
        self.opted_in_guest(self.org, "one@example.com")
        self.opted_in_guest(self.org, "two@example.com")
        campaign = self.make_campaign(self.org)

        self.client.post(
            f"/dashboard/campaigns/{campaign.pk}/send/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(CampaignSend.objects.filter(campaign=campaign).count(), 2)

        # A second send on the now-SENDING campaign is refused (CampaignStateError),
        # flashed rather than 500ing, and creates no new rows.
        resp = self.client.post(
            f"/dashboard/campaigns/{campaign.pk}/send/",
            HTTP_HOST=host_for("roxy"),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        message_texts = [m.message for m in resp.context["messages"]]
        self.assertTrue(
            any("can't be sent again" in t for t in message_texts), message_texts
        )
        self.assertEqual(CampaignSend.objects.filter(campaign=campaign).count(), 2)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, EmailCampaign.Status.SENDING)

    def test_send_cross_org_404s_and_creates_nothing(self):
        self.opted_in_guest(self.other_org, "theirs@example.com")
        other = self.make_campaign(self.other_org)
        resp = self.client.post(
            f"/dashboard/campaigns/{other.pk}/send/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(CampaignSend.objects.filter(campaign=other).count(), 0)
        other.refresh_from_db()
        self.assertEqual(other.status, EmailCampaign.Status.DRAFT)


class CampaignDetailTests(StaffFixtureMixin, CampaignFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_draft_detail_shows_live_recipient_preview(self):
        self.opted_in_guest(self.org, "a@example.com")
        self.opted_in_guest(self.org, "b@example.com")
        campaign = self.make_campaign(self.org)

        resp = self.client.get(
            f"/dashboard/campaigns/{campaign.pk}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["recipient_preview"], 2)

    def test_detail_renders_for_campaign_with_no_recipients(self):
        # A blank-name / no-guest org must not 500 the detail page.
        campaign = self.make_campaign(self.org)
        resp = self.client.get(
            f"/dashboard/campaigns/{campaign.pk}/", HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["recipient_preview"], 0)
