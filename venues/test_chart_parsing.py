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
        "row_alignment": "edge",
        "offset_mode": "repeated",
        "row_x_offset": 0.0,
        "alt_row_seat_delta": 0,
        "numbering_scheme": "sequential",
        "seat_number_base": 0,
        "row_label_scheme": "skip_io",
        "row_label_start": 0,
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
        usage=SimpleNamespace(
            input_tokens=4182,
            output_tokens=1905,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
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

    def test_usage_is_attached_and_describable(self):
        # Default parse is two passes (extract + verify) -- usage sums both.
        client = fake_client(fake_response(chart_spec()))
        with mock.patch.object(chart_parsing, "_get_client", return_value=client):
            spec = parse_chart_file(b"fake", "image/png")
        self.assertEqual(client.messages.create.call_count, 2)
        self.assertEqual(spec["usage"]["input_tokens"], 8364)
        self.assertEqual(spec["usage"]["output_tokens"], 3810)
        self.assertIn("8,364 tokens in", chart_parsing.describe_usage(spec["usage"]))
        self.assertIn("3,810 out", chart_parsing.describe_usage(spec["usage"]))
        # A spec carrying usage still builds (validate ignores unknown keys).
        org = make_org("usage")
        venue = Venue.objects.create(organization=org, name="Stage")
        build_chart_from_spec(venue, spec)

    def test_verify_pass_sends_first_pass_spec_back(self):
        client = fake_client(fake_response(chart_spec()))
        with mock.patch.object(chart_parsing, "_get_client", return_value=client):
            parse_chart_file(b"fake", "image/png")
        second_prompt = client.messages.create.call_args_list[1].kwargs["messages"][0]["content"][1]["text"]
        self.assertIn("Re-examine the chart", second_prompt)
        self.assertIn('"chart_name": "Main house"', second_prompt)
        # The image rides along on the verify pass too.
        second_file_block = client.messages.create.call_args_list[1].kwargs["messages"][0]["content"][0]
        self.assertEqual(second_file_block["type"], "image")

    def test_verify_false_is_a_single_pass(self):
        client = fake_client(fake_response(chart_spec()))
        with mock.patch.object(chart_parsing, "_get_client", return_value=client):
            spec = parse_chart_file(b"fake", "image/png", verify=False)
        self.assertEqual(client.messages.create.call_count, 1)
        self.assertEqual(spec["usage"]["input_tokens"], 4182)

    def test_describe_usage_is_empty_when_unknown(self):
        # A response with no usage (or a caller without a parse) renders
        # nothing rather than fake zeros.
        self.assertEqual(chart_parsing.describe_usage(None), "")
        self.assertEqual(
            chart_parsing.describe_usage({"model": "claude-opus-4-8", "input_tokens": None}), ""
        )

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

    def test_center_alignment_derives_a_recentering_offset(self):
        # A center block that widens toward the back (widths 4, 5, 6 in a
        # 6-wide grid, trimmed from the high-number end) is left-aligned by
        # the removals alone; row_alignment="center" folds in the offset
        # that re-centers it: -seat_pitch/2 * (per-row widening) = -0.5.
        removed = [["A", "5"], ["A", "6"], ["B", "6"]]
        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    rows=3, seats_per_row=6, row_alignment="center", removed_seats=removed
                )
            )
        )
        self.assertEqual(spec["sections"][0]["row_x_offset"], -0.5)
        # Idempotent: re-validating the folded spec doesn't double-apply.
        revalidated = validate_chart_spec(spec)
        self.assertEqual(revalidated["sections"][0]["row_x_offset"], -0.5)

    def test_center_alignment_ignores_outlier_back_row(self):
        # The slope anchors on the WIDEST row, so a single odd back row (a
        # mix-desk cut-out) doesn't flip the derived offset: widths 4, 5, 6,
        # 2 still derive from rows A->C.
        removed = [["A", "5"], ["A", "6"], ["B", "6"], ["D", "3"], ["D", "4"], ["D", "5"], ["D", "6"]]
        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    rows=4, seats_per_row=6, row_alignment="center", removed_seats=removed
                )
            )
        )
        self.assertEqual(spec["sections"][0]["row_x_offset"], -0.5)

    def test_center_alignment_attempts_nothing_without_a_clear_taper(self):
        # Oscillating widths (5, 4, 6, 3, 5 in a 6-wide grid) fit no linear
        # taper -- the derivation declines rather than applying an offset
        # that would merely look deliberate. Same when the section uses
        # ALTERNATING stagger, which is its own mechanism.
        removed = [["A", "6"], ["B", "5"], ["B", "6"], ["D", "4"], ["D", "5"], ["D", "6"], ["E", "6"]]
        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    rows=5, seats_per_row=6, row_alignment="center", removed_seats=removed
                )
            )
        )
        self.assertEqual(spec["sections"][0]["row_x_offset"], 0.0)

        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    rows=3, seats_per_row=6, row_alignment="center",
                    offset_mode="alternating", alt_row_seat_delta=-1,
                )
            )
        )
        self.assertEqual(spec["sections"][0]["row_x_offset"], 0.0)

    def test_edge_alignment_and_explicit_offsets_are_untouched(self):
        # Default/edge alignment never derives an offset...
        spec = validate_chart_spec(
            chart_spec(section_spec(rows=3, seats_per_row=6, removed_seats=[["A", "6"]]))
        )
        self.assertEqual(spec["sections"][0]["row_x_offset"], 0.0)
        # ...and a centered section whose parse already reported an offset
        # (centered AND raked) keeps the reported value.
        spec = validate_chart_spec(
            chart_spec(
                section_spec(
                    rows=3, seats_per_row=6, row_alignment="center",
                    row_x_offset=0.75, removed_seats=[["A", "6"]],
                )
            )
        )
        self.assertEqual(spec["sections"][0]["row_x_offset"], 0.75)

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


# --- real-world chart transcription (regression fixture) -------------------
#
# A faithful transcription of a real three-tier proscenium house chart
# (Orchestra A-M / Parterre N-U / Balcony V-Z, each split Left/Center/Right):
# left blocks odd-descending toward the aisle, right blocks even-ascending,
# center blocks continental "hundreds_flat" (every row restarts at 101), row
# letters continuing across the tiers (row_label_start), ragged rows via
# removed_seats, and ten wheelchair positions. This is the shape the AI
# parse should produce for that chart, and it exercises every labeling/
# numbering feature at once -- kept as a permanent inventory regression.


def _house_section(
    name, tier, *, numbering, width, start, keeps, origin, rotation=0.0,
    row_alignment="edge", accessible=(),
):
    """One section of the transcribed house. `keeps` is front-to-back, one
    entry per row: an int n keeps the n aisle-most seats of a `width`-wide
    row (0 = the row doesn't exist in this section), a list keeps exactly
    those printed numbers. Everything else becomes removed_seats."""
    from venues.generation import generate_row_labels

    labels = generate_row_labels(len(keeps), Section.RowLabelScheme.SKIP_IO, start)
    if numbering == "odd_desc_left":
        full = [2 * (width - i) - 1 for i in range(width)]  # aisle (1) is rightmost
        kept_for = lambda keep: full[width - keep:] if isinstance(keep, int) else keep
    elif numbering == "even_asc_right":
        full = [2 * (i + 1) for i in range(width)]  # aisle (2) is leftmost
        kept_for = lambda keep: full[:keep] if isinstance(keep, int) else keep
    else:  # hundreds_flat center block
        full = [101 + i for i in range(width)]
        kept_for = lambda keep: full[:keep] if isinstance(keep, int) else keep

    removed = []
    for label, keep in zip(labels, keeps):
        kept = {str(n) for n in kept_for(keep)}
        removed.extend([label, str(n)] for n in full if str(n) not in kept)

    return section_spec(
        name=name,
        tier=tier,
        rows=len(keeps),
        seats_per_row=width,
        numbering_scheme=numbering,
        row_label_start=start,
        origin_x=origin[0],
        origin_y=origin[1],
        rotation=rotation,
        row_alignment=row_alignment,
        removed_seats=removed,
        accessible_seats=[list(identity) for identity in accessible],
    )


def real_world_chart_spec():
    orchestra_side = [0, 5, 8, 9, 9, 9, 9, 9, 9, 10, 10, 8]  # rows A-M
    return {
        "chart_name": "Main house",
        "sections": [
            _house_section(
                "Orchestra Left", "Orchestra", numbering="odd_desc_left", width=10, start=0,
                keeps=orchestra_side, origin=(-14.3, 0.0),
                accessible=[("M", "7"), ("M", "5")],
            ),
            _house_section(
                "Orchestra Center", "Orchestra", numbering="hundreds_flat", width=15, start=0,
                # Row D is a cross-aisle gap in the printed chart.
                keeps=[9, 10, 11, 0, 11, 12, 13, 12, 13, 14, 15, 12],
                origin=(0.0, 0.0), row_alignment="center",
                accessible=[("M", "101"), ("M", "106"), ("M", "107"), ("M", "112")],
            ),
            _house_section(
                "Orchestra Right", "Orchestra", numbering="even_asc_right", width=10, start=0,
                keeps=orchestra_side, origin=(12.7, 0.0),
                accessible=[("M", "6"), ("M", "8")],
            ),
            _house_section(
                "Parterre Left", "Parterre", numbering="odd_desc_left", width=10, start=12,
                keeps=[10, 10, 9, 8, 8, 7, 3], origin=(-18.3, 13.5),
                accessible=[("U", "1")],
            ),
            _house_section(
                "Parterre Center", "Parterre", numbering="hundreds_flat", width=21, start=12,
                # Row U keeps only its two flanks (mix desk in the middle).
                keeps=[18, 19, 18, 19, 20, 21, [101, 102, 117, 118, 119, 120]],
                origin=(-6.3, 13.5), row_alignment="center",
                accessible=[("U", "101")],
            ),
            _house_section(
                "Parterre Right", "Parterre", numbering="even_asc_right", width=10, start=12,
                keeps=[10, 10, 9, 8, 8, 7, 5], origin=(15.7, 13.5),
            ),
            _house_section(
                "Balcony Left", "Balcony", numbering="odd_desc_left", width=7, start=19,
                keeps=[7, 7, 7, 7, 7], origin=(-12.3, 23.0),
            ),
            _house_section(
                "Balcony Center", "Balcony", numbering="hundreds_flat", width=15, start=19,
                keeps=[14, 13, 15, 13, 14], origin=(-3.3, 23.0), row_alignment="center",
            ),
            _house_section(
                "Balcony Right", "Balcony", numbering="even_asc_right", width=7, start=19,
                keeps=[7, 7, 7, 7, 7], origin=(12.7, 23.0),
            ),
            # The angled entrance boxes beside the balcony: modelled as
            # one-row sections wide enough that the printed numbers (15/17/19
            # and 16/18/20) are real identities, with the aisle-side seats
            # removed. Row letter V is a best guess -- the chart doesn't
            # label them.
            _house_section(
                "Balcony Left Entrance", "Balcony", numbering="odd_desc_left", width=10, start=19,
                keeps=[[15, 17, 19]], origin=(-19.0, 20.5), rotation=-45.0,
            ),
            _house_section(
                "Balcony Right Entrance", "Balcony", numbering="even_asc_right", width=10, start=19,
                keeps=[[16, 18, 20]], origin=(24.0, 20.5), rotation=45.0,
            ),
        ],
    }


class RealWorldChartTests(TestCase):
    """Inventory regression for the transcribed three-tier house above."""

    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.chart = build_chart_from_spec(self.venue, real_world_chart_spec())

    def test_per_section_seat_counts(self):
        counts = {s.name: s.seats.count() for s in self.chart.sections.all()}
        self.assertEqual(
            counts,
            {
                "Orchestra Left": 95,
                "Orchestra Center": 132,
                "Orchestra Right": 95,
                "Parterre Left": 55,
                "Parterre Center": 121,
                "Parterre Right": 57,
                "Balcony Left": 35,
                "Balcony Center": 69,
                "Balcony Right": 35,
                "Balcony Left Entrance": 3,
                "Balcony Right Entrance": 3,
            },
        )
        self.assertEqual(Seat.objects.filter(section__chart=self.chart).count(), 700)

    def test_row_labels_continue_across_tiers(self):
        def labels(section_name):
            section = self.chart.sections.get(name=section_name)
            return sorted(set(section.seats.values_list("row_label", flat=True)))

        # Orchestra A-M with I skipped; center D is a cross-aisle gap.
        self.assertEqual(
            labels("Orchestra Left"),
            ["B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M"],
        )
        self.assertEqual(
            labels("Orchestra Center"),
            ["A", "B", "C", "E", "F", "G", "H", "J", "K", "L", "M"],
        )
        # Parterre continues at N (skipping O), Balcony at V.
        self.assertEqual(labels("Parterre Center"), ["N", "P", "Q", "R", "S", "T", "U"])
        self.assertEqual(labels("Balcony Center"), ["V", "W", "X", "Y", "Z"])

    def test_continental_numbering_and_ragged_rows(self):
        center = self.chart.sections.get(name="Parterre Center")
        row_n = sorted(
            (int(n) for n in center.seats.filter(row_label="N").values_list("number", flat=True))
        )
        self.assertEqual(row_n, list(range(101, 119)))  # 101-118, restarting at 101
        row_u = sorted(
            (int(n) for n in center.seats.filter(row_label="U").values_list("number", flat=True))
        )
        self.assertEqual(row_u, [101, 102, 117, 118, 119, 120])  # mix-desk gap

    def test_center_blocks_get_recentering_offsets(self):
        # The center blocks are reported as row_alignment="center" and get
        # their re-centering row_x_offset derived from the row widths --
        # front row vs widest row, /2 (see _derived_center_offset).
        offsets = {
            s.name: s.row_x_offset
            for s in self.chart.sections.filter(name__endswith="Center")
        }
        self.assertEqual(
            offsets,
            {
                "Orchestra Center": -0.3,  # steady taper A=9 -> L=15 over 10 rows
                "Parterre Center": -0.3,   # steady taper N=18 -> T=21 over 5 rows
                # Balcony widths oscillate (14, 13, 15, 13, 14) -- no clear
                # taper, so the derivation declines and attempts nothing.
                "Balcony Center": 0.0,
            },
        )
        # Side blocks stay edge-aligned with no offset.
        self.assertEqual(self.chart.sections.get(name="Orchestra Left").row_x_offset, 0.0)

    def test_wheelchair_inventory(self):
        accessible = {
            (seat.section.name, seat.row_label, seat.number)
            for seat in Seat.objects.filter(
                section__chart=self.chart, is_accessible=True
            ).select_related("section")
        }
        self.assertEqual(
            accessible,
            {
                ("Orchestra Left", "M", "5"),
                ("Orchestra Left", "M", "7"),
                ("Orchestra Center", "M", "101"),
                ("Orchestra Center", "M", "106"),
                ("Orchestra Center", "M", "107"),
                ("Orchestra Center", "M", "112"),
                ("Orchestra Right", "M", "6"),
                ("Orchestra Right", "M", "8"),
                ("Parterre Left", "U", "1"),
                ("Parterre Center", "U", "101"),
            },
        )


# --- background jobs (run_parse_job / run_chart_parse) ----------------------

import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from venues.chart_parsing import run_parse_job
from venues.models import ChartParseJob

MEDIA_TMP = tempfile.mkdtemp(prefix="boxoffice-test-media-")


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class RunParseJobTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")

    def make_job(self, **overrides):
        fields = {
            "organization": self.org,
            "venue": self.venue,
            "upload": SimpleUploadedFile("house.png", b"fake-png", content_type="image/png"),
            "media_type": "image/png",
        }
        fields.update(overrides)
        return ChartParseJob.objects.create(**fields)

    def test_success_builds_chart_and_records_outcome(self):
        job = self.make_job(chart_name="Cabaret setup")
        client = fake_client(fake_response(chart_spec()))
        with mock.patch.object(chart_parsing, "_get_client", return_value=client):
            result = run_parse_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, ChartParseJob.Status.SUCCEEDED)
        self.assertEqual(job.chart.name, "Cabaret setup")
        self.assertEqual(job.usage["input_tokens"], 8364)  # both passes summed
        self.assertEqual(job.progress, "")
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(result.pk, job.pk)
        self.assertEqual(Seat.objects.filter(section__chart=job.chart).count(), 12)

    def test_replace_target_rebuilds_that_chart_in_place(self):
        chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Main house"
        )
        Section.objects.create(organization=self.org, chart=chart, name="Old section")
        job = self.make_job(replace_chart=chart)
        with mock.patch.object(
            chart_parsing, "_get_client", return_value=fake_client(fake_response(chart_spec()))
        ):
            run_parse_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, ChartParseJob.Status.SUCCEEDED)
        self.assertEqual(job.chart_id, chart.pk)  # identity survives the replace
        self.assertEqual(
            list(chart.sections.values_list("name", flat=True)), ["Orchestra"]
        )

    def test_parsing_error_marks_failed_with_staff_safe_message(self):
        job = self.make_job()
        client = fake_client(fake_response(chart_spec(), stop_reason="refusal"))
        with mock.patch.object(chart_parsing, "_get_client", return_value=client):
            run_parse_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, ChartParseJob.Status.FAILED)
        self.assertIn("declined", job.error)
        self.assertIsNone(job.chart)

    def test_unexpected_crash_is_caught_not_raised(self):
        job = self.make_job()
        with mock.patch.object(chart_parsing, "_get_client", side_effect=RuntimeError("boom")):
            run_parse_job(job.pk)  # must not raise -- nobody catches in the worker
        job.refresh_from_db()
        self.assertEqual(job.status, ChartParseJob.Status.FAILED)
        self.assertIn("Unexpected error", job.error)

    def test_only_pending_jobs_are_claimable(self):
        job = self.make_job(status=ChartParseJob.Status.RUNNING)
        self.assertIsNone(run_parse_job(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, ChartParseJob.Status.RUNNING)  # untouched

    def test_progress_stages_are_written_to_the_row(self):
        job = self.make_job()
        stages = []
        real_parse = chart_parsing.parse_chart_file

        def spying_parse(data, media_type, **kwargs):
            on_progress = kwargs.get("on_progress")
            with mock.patch.object(
                chart_parsing, "_get_client", return_value=fake_client(fake_response(chart_spec()))
            ):
                spec = real_parse(data, media_type, on_progress=on_progress)
            return spec

        original_on_progress_write = ChartParseJob.save

        def spying_save(self, *args, **kwargs):
            if kwargs.get("update_fields") == ["progress"]:
                stages.append(self.progress)
            return original_on_progress_write(self, *args, **kwargs)

        with mock.patch.object(chart_parsing, "parse_chart_file", side_effect=spying_parse):
            with mock.patch.object(ChartParseJob, "save", spying_save):
                run_parse_job(job.pk)
        self.assertEqual(stages, ["extracting", "verifying", "building"])

    def test_worker_command_runs_a_job_end_to_end(self):
        from django.core.management import CommandError, call_command
        from io import StringIO

        job = self.make_job()
        buf = StringIO()
        with mock.patch.object(
            chart_parsing, "_get_client", return_value=fake_client(fake_response(chart_spec()))
        ):
            call_command("run_chart_parse", str(job.pk), stdout=buf)
        job.refresh_from_db()
        self.assertEqual(job.status, ChartParseJob.Status.SUCCEEDED)
        self.assertIn("succeeded", buf.getvalue())

        with self.assertRaises(CommandError):
            call_command("run_chart_parse", "999999", stdout=StringIO())
