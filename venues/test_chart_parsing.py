"""Tests for venues/chart_parsing.py -- the image/PDF -> SeatingChart
pipeline. The Claude API call is always mocked (patch _get_client); what's
under test is everything around it: media-type sniffing, content-block
shape, response handling (refusal/truncation/garbage), spec normalisation
(clamps, enum fallbacks, name dedupe), and that build_chart_from_spec
persists sections whose generated seats match venues.generation exactly."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from events.models import Event, Performance
from orders.models import Order, Ticket
from venues import chart_parsing
from venues.chart_parsing import (
    ChartParsingError,
    build_chart_from_spec,
    media_type_for_upload,
    parse_chart_file,
    validate_chart_spec,
)
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org


def section_spec(**overrides):
    """A fully-populated, valid section spec (the shape the structured-output
    schema forces), overridable per test."""
    spec = {
        "name": "Orchestra",
        "tier": "Orchestra",
        "rows": 3,
        "seats_per_row": 4,
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
        "removed_seats": [],
        "accessible_seats": [],
    }
    spec.update(overrides)
    return spec


def chart_spec(*sections, name="Main house"):
    return {"chart_name": name, "sections": list(sections) or [section_spec()]}


def fake_response(spec, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(spec))],
    )


def fake_client(response):
    client = mock.Mock()
    client.messages.create.return_value = response
    return client


class MediaTypeTests(TestCase):
    def test_prefers_supported_content_type(self):
        self.assertEqual(media_type_for_upload("whatever.bin", "image/png"), "image/png")

    def test_falls_back_to_extension(self):
        self.assertEqual(
            media_type_for_upload("house.PDF", "application/octet-stream"), "application/pdf"
        )
        self.assertEqual(media_type_for_upload("chart.jpeg"), "image/jpeg")

    def test_unsupported_returns_none(self):
        self.assertIsNone(media_type_for_upload("chart.svg", "image/svg+xml"))
        self.assertIsNone(media_type_for_upload(None, None))


class ParseChartFileTests(TestCase):
    def test_rejects_unsupported_media_type_and_bad_sizes(self):
        with self.assertRaises(ChartParsingError):
            parse_chart_file(b"x", "image/svg+xml")
        with self.assertRaises(ChartParsingError):
            parse_chart_file(b"", "image/png")
        with self.assertRaises(ChartParsingError):
            parse_chart_file(b"x" * (chart_parsing.MAX_UPLOAD_BYTES + 1), "image/png")

    def test_image_and_pdf_content_blocks(self):
        """Images go to the API as an `image` block, PDFs as a `document`
        block -- both base64 -- and the reply comes back as a validated spec."""
        for media_type, expected_block in (
            ("image/png", "image"),
            ("application/pdf", "document"),
        ):
            client = fake_client(fake_response(chart_spec()))
            with mock.patch.object(chart_parsing, "_get_client", return_value=client):
                spec = parse_chart_file(b"fake-bytes", media_type)
            self.assertEqual(spec["sections"][0]["name"], "Orchestra")
            kwargs = client.messages.create.call_args.kwargs
            file_block = kwargs["messages"][0]["content"][0]
            self.assertEqual(file_block["type"], expected_block)
            self.assertEqual(file_block["source"]["media_type"], media_type)
            self.assertEqual(file_block["source"]["type"], "base64")
            # Structured output pinned to our schema, so json.loads is safe.
            self.assertEqual(
                kwargs["output_config"]["format"]["schema"], chart_parsing.CHART_SPEC_SCHEMA
            )

    def test_refusal_and_truncation_are_clear_errors(self):
        for stop_reason in ("refusal", "max_tokens"):
            client = fake_client(fake_response(chart_spec(), stop_reason=stop_reason))
            with mock.patch.object(chart_parsing, "_get_client", return_value=client):
                with self.assertRaises(ChartParsingError):
                    parse_chart_file(b"fake", "image/png")

    def test_unparseable_reply_is_a_clear_error(self):
        response = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")]
        )
        with mock.patch.object(chart_parsing, "_get_client", return_value=fake_client(response)):
            with self.assertRaises(ChartParsingError):
                parse_chart_file(b"fake", "image/png")


class ValidateChartSpecTests(TestCase):
    def test_empty_or_garbage_specs_raise(self):
        for bad in (None, [], {}, {"sections": []}, {"sections": "nope"}):
            with self.assertRaises(ChartParsingError):
                validate_chart_spec(bad)

    def test_clamps_counts_and_coerces_types(self):
        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    rows=99999,
                    seats_per_row=0,
                    seat_pitch="not-a-number",
                    arc_radius=-5,
                    origin_x="3.5",
                )
            )
        )
        section = spec["sections"][0]
        self.assertEqual(section["rows"], chart_parsing.MAX_ROWS)
        self.assertEqual(section["seats_per_row"], 1)
        self.assertEqual(section["seat_pitch"], 1.0)
        self.assertIsNone(section["arc_radius"])  # non-positive radius -> straight
        self.assertEqual(section["origin_x"], 3.5)

    def test_unknown_enums_fall_back_to_defaults(self):
        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    numbering_scheme="roman-numerals",
                    row_label_scheme="emoji",
                    offset_mode="diagonal",
                )
            )
        )
        section = spec["sections"][0]
        self.assertEqual(section["numbering_scheme"], Section.NumberingScheme.SEQUENTIAL)
        self.assertEqual(section["row_label_scheme"], Section.RowLabelScheme.SKIP_IO)
        self.assertEqual(section["offset_mode"], Section.OffsetMode.REPEATED)

    def test_duplicate_and_blank_section_names(self):
        spec = validate_chart_spec(
            chart_spec(
                section_spec(name="Balcony"),
                section_spec(name="Balcony"),
                section_spec(name=""),
            )
        )
        names = [s["name"] for s in spec["sections"]]
        self.assertEqual(names, ["Balcony", "Balcony (2)", "Section 3"])

    def test_malformed_seat_identities_are_dropped(self):
        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    removed_seats=[["A", 1], "garbage", ["B"], ["C", "2", "extra"]],
                    accessible_seats="nope",
                )
            )
        )
        section = spec["sections"][0]
        self.assertEqual(section["removed_seats"], [["A", "1"]])
        self.assertEqual(section["accessible_seats"], [])

    def test_blank_chart_name_gets_default(self):
        spec = validate_chart_spec({"chart_name": "  ", "sections": [section_spec()]})
        self.assertEqual(spec["chart_name"], "Parsed chart")


class BuildChartFromSpecTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")

    def test_builds_sections_and_generated_seats(self):
        spec = chart_spec(
            section_spec(name="Orchestra", rows=2, seats_per_row=3),
            section_spec(
                name="Balcony",
                tier="Balcony",
                rows=2,
                seats_per_row=4,
                origin_y=10.0,
                arc_radius=20.0,
                numbering_scheme="odd_desc_left",
                removed_seats=[["A", "7"]],
                accessible_seats=[["A", "1"]],
            ),
        )
        chart = build_chart_from_spec(self.venue, spec)

        self.assertEqual(chart.organization, self.org)
        self.assertEqual(chart.venue, self.venue)
        self.assertEqual(chart.name, "Main house")

        orchestra, balcony = chart.sections.order_by("ordering")
        self.assertEqual((orchestra.name, orchestra.ordering), ("Orchestra", 0))
        self.assertEqual((balcony.name, balcony.ordering), ("Balcony", 1))
        self.assertEqual(balcony.arc_radius, 20.0)
        self.assertEqual(balcony.removed_seats, [["A", "7"]])

        # Orchestra: plain 2x3 grid, sequential numbers.
        self.assertEqual(orchestra.seats.count(), 6)
        # Balcony: 2x4 minus the removed A7 (odd_desc_left numbers a 4-seat
        # row 7,5,3,1 so "7" is a real identity), with A1 flagged accessible.
        self.assertEqual(balcony.seats.count(), 7)
        self.assertFalse(balcony.seats.filter(row_label="A", number="7").exists())
        self.assertTrue(balcony.seats.get(row_label="A", number="1").is_accessible)

        # Every seat is stamped with the venue's org -- never the JSON's say-so.
        self.assertEqual(
            Seat.objects.filter(section__chart=chart).exclude(organization=self.org).count(), 0
        )

    def test_name_collision_without_replace_refuses(self):
        SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Main house")
        with self.assertRaises(ChartParsingError):
            build_chart_from_spec(self.venue, chart_spec())

    def test_replace_rebuilds_in_place(self):
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Main house")
        Section.objects.create(organization=self.org, chart=chart, name="Old section")
        rebuilt = build_chart_from_spec(self.venue, chart_spec(), replace=True)
        self.assertEqual(rebuilt.pk, chart.pk)  # identity survives, contents replaced
        self.assertEqual(list(rebuilt.sections.values_list("name", flat=True)), ["Orchestra"])

    def test_replace_refuses_over_live_tickets(self):
        chart = build_chart_from_spec(self.venue, chart_spec())
        seat = Seat.objects.filter(section__chart=chart).first()
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at="2030-01-01T20:00:00Z",
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        order = Order.objects.create(
            organization=self.org, performance=performance, buyer_email="a@b.c", total="10.00"
        )
        Ticket.objects.create(
            organization=self.org, order=order, performance=performance, seat=seat
        )
        with self.assertRaises(ChartParsingError):
            build_chart_from_spec(self.venue, chart_spec(), replace=True)

    def test_explicit_name_overrides_spec(self):
        chart = build_chart_from_spec(self.venue, chart_spec(), name="Cabaret setup")
        self.assertEqual(chart.name, "Cabaret setup")
