"""Thin CLI-layer tests for export_seating_chart / import_seating_chart --
the actual export/import logic is covered in venues/test_chart_io.py; these
just confirm the commands wire args -> venues.chart_io correctly."""

import json
import os

from django.core.management import CommandError, call_command
from django.test import TestCase

from venues.generation import generate_seats
from venues.models import SeatingChart, Section, Venue
from venues.tests import make_org


class ManagementCommandTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Standard"
        )
        section = Section.objects.create(organization=self.org, chart=self.chart, name="Orchestra")
        generate_seats(section, [3])

    def test_export_to_stdout(self):
        from io import StringIO

        buf = StringIO()
        call_command("export_seating_chart", str(self.chart.pk), stdout=buf)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["chart"]["name"], "Standard")
        self.assertEqual(len(data["sections"][0]["rows"][0]["seats"]), 3)

    def test_export_unknown_chart_errors(self):
        from io import StringIO

        with self.assertRaises(CommandError):
            call_command("export_seating_chart", "999999", stdout=StringIO())

    def test_export_then_import_round_trip_via_file(self):
        import tempfile
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "chart.json")
            call_command("export_seating_chart", str(self.chart.pk), "--output", out_file, stdout=StringIO())
            self.assertTrue(os.path.exists(out_file))

            other_venue = Venue.objects.create(organization=self.org, name="Second Stage")
            buf = StringIO()
            call_command(
                "import_seating_chart", out_file, "--venue", str(other_venue.pk), stdout=buf
            )
            imported = SeatingChart.objects.get(organization=self.org, venue=other_venue, name="Standard")
            self.assertEqual(imported.sections.count(), 1)
            self.assertEqual(imported.sections.first().seats.count(), 3)

    def test_import_unknown_venue_errors(self):
        import tempfile
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "chart.json")
            call_command("export_seating_chart", str(self.chart.pk), "--output", out_file, stdout=StringIO())
            with self.assertRaises(CommandError):
                call_command("import_seating_chart", out_file, "--venue", "999999", stdout=StringIO())

    def test_import_name_collision_without_replace_errors(self):
        import tempfile
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "chart.json")
            call_command("export_seating_chart", str(self.chart.pk), "--output", out_file, stdout=StringIO())
            with self.assertRaises(CommandError):
                call_command(
                    "import_seating_chart", out_file, "--venue", str(self.venue.pk), stdout=StringIO()
                )
