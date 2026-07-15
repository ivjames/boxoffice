"""HTTP-layer tests for the background "Import from image/PDF" flow:
dashboard.views.chart_parse_upload (creates a ChartParseJob + spawns the
detached worker -- spawn is mocked here) and chart_parse_status (the
polling endpoint). The worker itself is tested in
venues/test_chart_parsing.py; these cover role gating, tenant scoping,
upload validation, the AJAX/plain-form split, the editor's replace-into-
chart target, and the status payload shape."""

import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from venues import chart_parsing
from venues.models import ChartParseJob, SeatingChart, Section, Venue
from venues.tests import make_org

MEDIA_TMP = tempfile.mkdtemp(prefix="boxoffice-test-media-")


def png_upload(name="house.png", content=b"fake-png-bytes"):
    return SimpleUploadedFile(name, content, content_type="image/png")


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class ChartParseUploadTests(StaffFixtureMixin, TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.manager, self.password = self.make_staff(self.org, Membership.Role.MANAGER)
        self.host = host_for("roxy")
        self.url = f"/dashboard/venues/{self.venue.pk}/charts/parse/"
        self.client.force_login(self.manager)
        spawn_patch = mock.patch.object(chart_parsing, "spawn_parse_job")
        self.spawn = spawn_patch.start()
        self.addCleanup(spawn_patch.stop)

    def post_upload(self, ajax=False, **extra):
        data = {"file": png_upload()}
        data.update(extra)
        kwargs = {"HTTP_HOST": self.host}
        if ajax:
            kwargs["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return self.client.post(self.url, data, **kwargs)

    def test_upload_creates_job_and_spawns_worker(self):
        resp = self.post_upload(name="Cabaret setup")
        self.assertRedirects(
            resp, f"/dashboard/venues/{self.venue.pk}/charts/", fetch_redirect_response=False
        )
        job = ChartParseJob.objects.get()
        self.assertEqual(job.organization, self.org)
        self.assertEqual(job.venue, self.venue)
        self.assertEqual(job.media_type, "image/png")
        self.assertEqual(job.chart_name, "Cabaret setup")
        self.assertEqual(job.status, ChartParseJob.Status.PENDING)
        self.assertIsNone(job.replace_chart)
        self.assertEqual(job.created_by, self.manager)
        self.spawn.assert_called_once_with(job)
        # No chart yet -- the worker builds it.
        self.assertFalse(SeatingChart.objects.exists())

    def test_ajax_upload_returns_job_and_status_url(self):
        resp = self.post_upload(ajax=True)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        job = ChartParseJob.objects.get()
        self.assertTrue(data["ok"])
        self.assertEqual(data["job_id"], job.pk)
        self.assertEqual(data["status_url"], f"/dashboard/parse-jobs/{job.pk}/status/")

    def test_editor_replace_target_sets_replace_chart(self):
        chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Main house"
        )
        resp = self.post_upload(ajax=True, chart=str(chart.pk))
        self.assertTrue(resp.json()["ok"])
        job = ChartParseJob.objects.get()
        self.assertEqual(job.replace_chart, chart)

    def test_replace_target_must_belong_to_the_venue_and_org(self):
        other_org = make_org("bijou")
        other_venue = Venue.objects.create(organization=other_org, name="Bijou Stage")
        foreign_chart = SeatingChart.objects.create(
            organization=other_org, venue=other_venue, name="Foreign"
        )
        resp = self.post_upload(chart=str(foreign_chart.pk))
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ChartParseJob.objects.exists())

    def test_missing_file_and_bad_type_are_rejected(self):
        resp = self.client.post(self.url, {}, HTTP_HOST=self.host, follow=True)
        self.assertContains(resp, "Choose an image or PDF")

        resp = self.client.post(
            self.url,
            {"file": SimpleUploadedFile("chart.svg", b"<svg/>", content_type="image/svg+xml")},
            HTTP_HOST=self.host,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unsupported file type", resp.json()["error"])
        self.assertFalse(ChartParseJob.objects.exists())
        self.spawn.assert_not_called()

    def test_requires_manager_role(self):
        box_office, _ = self.make_staff(self.org, Membership.Role.BOX_OFFICE)
        self.client.force_login(box_office)
        resp = self.post_upload()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ChartParseJob.objects.exists())

    def test_cannot_target_another_tenants_venue(self):
        other_org = make_org("bijou")
        other_venue = Venue.objects.create(organization=other_org, name="Bijou Stage")
        resp = self.client.post(
            f"/dashboard/venues/{other_venue.pk}/charts/parse/",
            {"file": png_upload()},
            HTTP_HOST=self.host,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ChartParseJob.objects.exists())

    def test_upload_form_and_job_monitor_render_on_chart_list(self):
        self.post_upload()
        resp = self.client.get(
            f"/dashboard/venues/{self.venue.pk}/charts/", HTTP_HOST=self.host
        )
        self.assertContains(resp, "Import from an image or PDF")
        self.assertContains(resp, f"/dashboard/venues/{self.venue.pk}/charts/parse/")
        job = ChartParseJob.objects.get()
        self.assertContains(resp, f"/dashboard/parse-jobs/{job.pk}/status/")

    def test_import_panel_renders_in_the_editor(self):
        chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Main house"
        )
        resp = self.client.get(f"/dashboard/charts/{chart.pk}/editor/", HTTP_HOST=self.host)
        self.assertContains(resp, "Import from image / PDF")
        self.assertContains(resp, f"/dashboard/venues/{self.venue.pk}/charts/parse/")


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class ChartParseStatusTests(StaffFixtureMixin, TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.manager, _ = self.make_staff(self.org, Membership.Role.MANAGER)
        self.host = host_for("roxy")
        self.client.force_login(self.manager)

    def make_job(self, **overrides):
        fields = {
            "organization": self.org,
            "venue": self.venue,
            "upload": png_upload(),
            "media_type": "image/png",
        }
        fields.update(overrides)
        return ChartParseJob.objects.create(**fields)

    def status(self, job):
        return self.client.get(f"/dashboard/parse-jobs/{job.pk}/status/", HTTP_HOST=self.host)

    def test_running_job_reports_progress(self):
        job = self.make_job(status=ChartParseJob.Status.RUNNING, progress="verifying")
        data = self.status(job).json()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["progress"], "verifying")
        self.assertIsNone(data["editor_url"])

    def test_succeeded_job_links_to_the_editor(self):
        chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Parsed"
        )
        Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        job = self.make_job(
            status=ChartParseJob.Status.SUCCEEDED,
            chart=chart,
            usage={"model": "claude-opus-4-8", "input_tokens": 8364, "output_tokens": 3810},
        )
        data = self.status(job).json()
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["editor_url"], f"/dashboard/charts/{chart.pk}/editor/")
        self.assertIn("8,364 tokens in", data["usage"])
        self.assertIn("1 section(s)", data["detail"])

    def test_stale_running_job_reports_failed(self):
        from datetime import timedelta

        from django.utils import timezone

        job = self.make_job(
            status=ChartParseJob.Status.RUNNING,
            started_at=timezone.now() - timedelta(minutes=ChartParseJob.STALE_AFTER_MINUTES + 1),
        )
        data = self.status(job).json()
        self.assertEqual(data["status"], "failed")
        self.assertIn("stopped responding", data["error"])

    def test_scoped_to_the_org(self):
        other_org = make_org("bijou")
        other_venue = Venue.objects.create(organization=other_org, name="Bijou Stage")
        foreign_job = ChartParseJob.objects.create(
            organization=other_org,
            venue=other_venue,
            upload=png_upload(),
            media_type="image/png",
        )
        resp = self.status(foreign_job)
        self.assertEqual(resp.status_code, 404)
