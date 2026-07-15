"""HTTP-layer tests for the chart list's "Import from image/PDF" flow
(dashboard.views.chart_parse_upload). The AI call itself is unit-tested in
venues/test_chart_parsing.py -- here parse_chart_file is mocked and the
tests cover role gating, tenant scoping, upload validation, the
success-redirect-into-editor path, and error flash messages."""

import json
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from venues import chart_parsing
from venues.models import Seat, SeatingChart, Venue
from venues.tests import make_org


def parsed_spec():
    return {
        "chart_name": "Main house",
        "sections": [
            {
                "name": "Orchestra",
                "tier": "",
                "rows": 2,
                "seats_per_row": 3,
                "origin_x": 0.0,
                "origin_y": 0.0,
                "rotation": 0.0,
                "seat_pitch": 1.0,
                "row_pitch": 1.0,
                "arc_radius": None,
                "offset_mode": "repeated",
                "row_x_offset": 0.0,
                "alt_row_seat_delta": 0,
                "numbering_scheme": "sequential",
                "row_label_scheme": "skip_io",
                "row_label_start": 0,
                "removed_seats": [],
                "accessible_seats": [],
            }
        ],
    }


def png_upload(name="house.png", content=b"fake-png-bytes"):
    return SimpleUploadedFile(name, content, content_type="image/png")


def fake_api_client(spec=None):
    """A mock Anthropic client whose messages.create returns a structured-
    output style response carrying `spec` -- patched over _get_client so the
    whole real pipeline below the API boundary runs."""
    client = mock.Mock()
    client.messages.create.return_value = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(spec or parsed_spec()))],
        usage=SimpleNamespace(
            input_tokens=4182,
            output_tokens=1905,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    return client


class ChartParseUploadTests(StaffFixtureMixin, TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.manager, self.password = self.make_staff(self.org, Membership.Role.MANAGER)
        self.host = host_for("roxy")
        self.url = f"/dashboard/venues/{self.venue.pk}/charts/parse/"
        self.client.force_login(self.manager)

    def post_upload(self, **extra):
        data = {"file": png_upload()}
        data.update(extra)
        return self.client.post(self.url, data, HTTP_HOST=self.host)

    def test_success_builds_chart_and_redirects_to_editor(self):
        with mock.patch.object(chart_parsing, "_get_client", return_value=fake_api_client()):
            resp = self.post_upload()
        chart = SeatingChart.objects.get(organization=self.org, venue=self.venue)
        self.assertEqual(chart.name, "Main house")
        self.assertEqual(Seat.objects.filter(section__chart=chart).count(), 6)
        self.assertRedirects(
            resp, f"/dashboard/charts/{chart.pk}/editor/", fetch_redirect_response=False
        )
        # The success flash reports the parse's token usage.
        editor = self.client.get(f"/dashboard/charts/{chart.pk}/editor/", HTTP_HOST=self.host)
        self.assertContains(editor, "4,182 tokens in")

    def test_optional_name_field_overrides_parsed_name(self):
        with mock.patch.object(chart_parsing, "_get_client", return_value=fake_api_client()):
            self.post_upload(name="Cabaret setup")
        self.assertTrue(
            SeatingChart.objects.filter(venue=self.venue, name="Cabaret setup").exists()
        )

    def test_missing_file_and_bad_type_flash_errors(self):
        resp = self.client.post(self.url, {}, HTTP_HOST=self.host, follow=True)
        self.assertContains(resp, "Choose an image or PDF")

        resp = self.client.post(
            self.url,
            {"file": SimpleUploadedFile("chart.svg", b"<svg/>", content_type="image/svg+xml")},
            HTTP_HOST=self.host,
            follow=True,
        )
        self.assertContains(resp, "Unsupported file type")
        self.assertFalse(SeatingChart.objects.exists())

    def test_parsing_error_flashes_message_and_creates_nothing(self):
        with mock.patch.object(
            chart_parsing,
            "parse_chart_file",
            side_effect=chart_parsing.ChartParsingError("The parsing service declined."),
        ):
            resp = self.post_upload()
        self.assertRedirects(
            resp, f"/dashboard/venues/{self.venue.pk}/charts/", fetch_redirect_response=False
        )
        self.assertFalse(SeatingChart.objects.exists())

    def test_requires_manager_role(self):
        box_office, _ = self.make_staff(self.org, Membership.Role.BOX_OFFICE)
        self.client.force_login(box_office)
        resp = self.post_upload()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(SeatingChart.objects.exists())

    def test_cannot_target_another_tenants_venue(self):
        other_org = make_org("bijou")
        other_venue = Venue.objects.create(organization=other_org, name="Bijou Stage")
        resp = self.client.post(
            f"/dashboard/venues/{other_venue.pk}/charts/parse/",
            {"file": png_upload()},
            HTTP_HOST=self.host,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(SeatingChart.objects.exists())

    def test_upload_form_renders_on_chart_list(self):
        resp = self.client.get(
            f"/dashboard/venues/{self.venue.pk}/charts/", HTTP_HOST=self.host
        )
        self.assertContains(resp, "Import from an image or PDF")
        self.assertContains(resp, f"/dashboard/venues/{self.venue.pk}/charts/parse/")
