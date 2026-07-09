"""Tests for events/zone_export.py (Phase D of the seating-chart epic,
docs/SEATING.md "D") and its `export_pricing_zones` management command:
render_zone_map returns valid PNG (Pillow-openable, expected page-size
pixel dimensions, the applied zone's color actually present as pixels) and
valid PDF (`%PDF-` header, non-trivial size) bytes; toggling
labels/legend/size changes the output; a zoneless/seatless performance
still renders instead of erroring; tenant isolation holds. See
events/test_zones.py for the zone CRUD service this reuses to set up
fixtures, and dashboard/tests.py's PricingZoneExportTests for the
HTTP-layer (role-gated, org-scoped, download-header) coverage."""

import io
import os
import tempfile
from decimal import Decimal

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from PIL import Image

from events.models import Event, Performance, PriceTier, ZoneTemplate
from events.zone_export import PAGE_SIZES_IN, PNG_DPI, ZoneExportError, render_zone_map
from events.zones import apply_zone
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


class ZoneExportFixtureMixin:
    def build_export_performance(self, org=None, subdomain="roxy"):
        self.org = org or make_org(subdomain)
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event = Event.objects.create(organization=self.org, title="A Show", slug="a-show")
        self.performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        self.chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.section = Section.objects.create(
            organization=self.org, chart=self.chart, name="Orchestra", seat_pitch=1.0, row_pitch=1.0
        )
        self.seats = []
        for row_index, row_label in enumerate("ABC"):
            for number in range(1, 6):
                seat = Seat.objects.create(
                    organization=self.org,
                    section=self.section,
                    row_label=row_label,
                    number=str(number),
                    x=float(number),
                    y=float(row_index),
                )
                self.seats.append(seat)
        PriceTier.objects.create(
            organization=self.org, section=self.section, name="Orchestra", amount=Decimal("50.00")
        )
        self.template = ZoneTemplate.objects.create(
            organization=self.org, name="Premium", color="#c1121f"
        )
        apply_zone(
            organization=self.org,
            performance=self.performance,
            seat_ids=[s.pk for s in self.seats[:5]],
            amount=Decimal("95.00"),
            template=self.template,
        )
        return self.performance


class RenderZoneMapPngTests(ZoneExportFixtureMixin, TestCase):
    def setUp(self):
        self.build_export_performance()

    def test_opens_in_pillow_with_expected_letter_size(self):
        content = render_zone_map(self.performance, fmt="png", size="letter")
        self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
        img = Image.open(io.BytesIO(content))
        self.assertEqual(img.format, "PNG")
        page_w_in, page_h_in = PAGE_SIZES_IN["letter"]
        self.assertEqual(img.size, (int(round(page_w_in * PNG_DPI)), int(round(page_h_in * PNG_DPI))))

    def test_legal_size_is_taller_than_letter_same_width(self):
        letter_img = Image.open(io.BytesIO(render_zone_map(self.performance, fmt="png", size="letter")))
        legal_img = Image.open(io.BytesIO(render_zone_map(self.performance, fmt="png", size="legal")))
        self.assertEqual(letter_img.size[0], legal_img.size[0])
        self.assertGreater(legal_img.size[1], letter_img.size[1])

    def test_sane_pixel_count_and_zone_color_present(self):
        content = render_zone_map(self.performance, fmt="png")
        img = Image.open(io.BytesIO(content)).convert("RGB")
        colors = img.getcolors(maxcolors=1_000_000)
        self.assertIsNotNone(colors)
        # White background + neutral unzoned seats + the applied zone's
        # color + text, at minimum.
        self.assertGreater(len(colors), 2)
        pixel_values = {rgb for _count, rgb in colors}
        self.assertIn(_hex_to_rgb(self.template.color), pixel_values)

    def test_labels_false_changes_output(self):
        with_labels = render_zone_map(self.performance, fmt="png", labels=True)
        without_labels = render_zone_map(self.performance, fmt="png", labels=False)
        self.assertNotEqual(with_labels, without_labels)

    def test_legend_false_changes_output(self):
        with_legend = render_zone_map(self.performance, fmt="png", legend=True)
        without_legend = render_zone_map(self.performance, fmt="png", legend=False)
        self.assertNotEqual(with_legend, without_legend)
        # Fewer distinct on-canvas colors once the legend swatches/text are
        # gone (still has the map's own colors, just not the legend's).
        img_with = Image.open(io.BytesIO(with_legend)).convert("RGB")
        img_without = Image.open(io.BytesIO(without_legend)).convert("RGB")
        self.assertGreaterEqual(len(img_with.getcolors(1_000_000)), 1)
        self.assertGreaterEqual(len(img_without.getcolors(1_000_000)), 1)

    def test_deterministic(self):
        first = render_zone_map(self.performance, fmt="png")
        second = render_zone_map(self.performance, fmt="png")
        self.assertEqual(first, second)

    def test_invalid_format_raises(self):
        with self.assertRaises(ZoneExportError):
            render_zone_map(self.performance, fmt="svg")

    def test_invalid_size_raises(self):
        with self.assertRaises(ZoneExportError):
            render_zone_map(self.performance, size="a4")


class RenderZoneMapPdfTests(ZoneExportFixtureMixin, TestCase):
    def setUp(self):
        self.build_export_performance()

    def test_starts_with_pdf_header_and_is_non_trivial(self):
        content = render_zone_map(self.performance, fmt="pdf")
        self.assertTrue(content.startswith(b"%PDF-"))
        self.assertGreater(len(content), 1000)

    def test_legal_bytes_differ_from_letter(self):
        letter_content = render_zone_map(self.performance, fmt="pdf", size="letter")
        legal_content = render_zone_map(self.performance, fmt="pdf", size="legal")
        self.assertNotEqual(letter_content, legal_content)

    def test_legend_off_changes_bytes(self):
        with_legend = render_zone_map(self.performance, fmt="pdf", legend=True)
        without_legend = render_zone_map(self.performance, fmt="pdf", legend=False)
        self.assertNotEqual(with_legend, without_legend)

    def test_labels_off_changes_bytes(self):
        with_labels = render_zone_map(self.performance, fmt="pdf", labels=True)
        without_labels = render_zone_map(self.performance, fmt="pdf", labels=False)
        self.assertNotEqual(with_labels, without_labels)

    def test_deterministic(self):
        first = render_zone_map(self.performance, fmt="pdf")
        second = render_zone_map(self.performance, fmt="pdf")
        self.assertEqual(first, second)


class RenderZoneMapEmptyPerformanceTests(TestCase):
    """A performance with no chart at all, and one with seats but no zones
    applied yet -- both must render gracefully instead of erroring."""

    def test_performance_with_no_chart_renders_png_and_pdf(self):
        org = make_org("roxy")
        venue = Venue.objects.create(organization=org, name="Main Stage")
        event = Event.objects.create(organization=org, title="Bare Show", slug="bare-show")
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        png = render_zone_map(performance, fmt="png")
        self.assertTrue(png.startswith(b"\x89PNG"))
        pdf = render_zone_map(performance, fmt="pdf")
        self.assertTrue(pdf.startswith(b"%PDF-"))

    def test_performance_with_seats_but_no_zones_renders(self):
        org = make_org("roxy")
        venue = Venue.objects.create(organization=org, name="Main Stage")
        event = Event.objects.create(organization=org, title="Unzoned Show", slug="unzoned-show")
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        chart = SeatingChart.objects.create(organization=org, venue=venue, name="Standard")
        section = Section.objects.create(organization=org, chart=chart, name="Orchestra")
        Seat.objects.create(organization=org, section=section, row_label="A", number="1", x=1.0, y=1.0)
        PriceTier.objects.create(organization=org, section=section, name="Orchestra", amount=Decimal("40.00"))

        png = render_zone_map(performance, fmt="png")
        img = Image.open(io.BytesIO(png)).convert("RGB")
        self.assertEqual(img.mode, "RGB")
        pdf = render_zone_map(performance, fmt="pdf")
        self.assertTrue(pdf.startswith(b"%PDF-"))


class RenderZoneMapTenantIsolationTests(ZoneExportFixtureMixin, TestCase):
    def test_other_orgs_zone_color_never_leaks_into_this_orgs_render(self):
        self.build_export_performance(subdomain="roxy")
        other_org = make_org("other")
        other_venue = Venue.objects.create(organization=other_org, name="Other Stage")
        other_event = Event.objects.create(organization=other_org, title="Other Show", slug="other-show")
        other_performance = Performance.objects.create(
            organization=other_org,
            event=other_event,
            venue=other_venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        other_chart = SeatingChart.objects.create(
            organization=other_org, venue=other_venue, name="Standard"
        )
        other_section = Section.objects.create(
            organization=other_org, chart=other_chart, name="Orchestra"
        )
        other_seats = [
            Seat.objects.create(
                organization=other_org, section=other_section, row_label="A", number=str(n), x=float(n), y=0.0
            )
            for n in range(1, 4)
        ]
        other_template = ZoneTemplate.objects.create(
            organization=other_org, name="Neon", color="#39ff14"
        )
        apply_zone(
            organization=other_org,
            performance=other_performance,
            seat_ids=[s.pk for s in other_seats],
            amount=Decimal("10.00"),
            template=other_template,
        )

        content = render_zone_map(self.performance, fmt="png")
        img = Image.open(io.BytesIO(content)).convert("RGB")
        pixel_values = {rgb for _count, rgb in img.getcolors(1_000_000)}
        self.assertNotIn(_hex_to_rgb(other_template.color), pixel_values)


class ExportPricingZonesCommandTests(ZoneExportFixtureMixin, TestCase):
    def setUp(self):
        self.build_export_performance()

    def test_writes_png_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "map.png")
            call_command("export_pricing_zones", str(self.performance.pk), "--out", out_path)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, "rb") as f:
                self.assertTrue(f.read(8).startswith(b"\x89PNG"))

    def test_writes_pdf_file_with_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "map.pdf")
            call_command(
                "export_pricing_zones",
                str(self.performance.pk),
                "--format",
                "pdf",
                "--size",
                "legal",
                "--no-labels",
                "--no-legend",
                "--out",
                out_path,
            )
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, "rb") as f:
                self.assertTrue(f.read(5) == b"%PDF-")

    def test_unknown_performance_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "map.png")
            with self.assertRaises(CommandError):
                call_command("export_pricing_zones", "999999", "--out", out_path)
