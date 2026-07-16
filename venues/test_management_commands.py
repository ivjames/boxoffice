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


class ParseSeatingChartCommandTests(TestCase):
    """CLI-layer tests for parse_seating_chart -- the parse pipeline itself
    is covered in venues/test_chart_parsing.py; these confirm arg wiring,
    --dry-run, and the usage line."""

    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")

    def _patched_client(self):
        import json as jsonlib
        from types import SimpleNamespace
        from unittest import mock

        from venues.test_chart_parsing import chart_spec

        client = mock.Mock()
        client.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=jsonlib.dumps(chart_spec()))],
            usage=SimpleNamespace(
                input_tokens=4182,
                output_tokens=1905,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )
        return client

    def _write_png(self, tmp_path):
        path = os.path.join(tmp_path, "house.png")
        with open(path, "wb") as f:
            f.write(b"fake-png-bytes")
        return path

    def test_parse_builds_chart_and_reports_usage(self):
        import tempfile
        from io import StringIO
        from unittest import mock

        from venues import chart_parsing

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_png(tmp)
            buf = StringIO()
            with mock.patch.object(chart_parsing, "_get_client", return_value=self._patched_client()):
                call_command("parse_seating_chart", path, "--venue", str(self.venue.pk), stdout=buf)
        output = buf.getvalue()
        self.assertIn("12 seat(s)", output)
        self.assertIn("8,364 tokens in", output)  # two passes
        self.assertTrue(SeatingChart.objects.filter(venue=self.venue, name="Main house").exists())

    def test_dry_run_prints_spec_and_creates_nothing(self):
        import tempfile
        from io import StringIO
        from unittest import mock

        from venues import chart_parsing

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_png(tmp)
            buf = StringIO()
            with mock.patch.object(chart_parsing, "_get_client", return_value=self._patched_client()):
                call_command(
                    "parse_seating_chart", path, "--venue", str(self.venue.pk), "--dry-run", stdout=buf
                )
        output = buf.getvalue()
        # The printed spec is valid JSON (usage stripped, reported separately).
        spec = json.loads(output[: output.rindex("}") + 1])
        self.assertEqual(spec["chart_name"], "Main house")
        self.assertNotIn("usage", spec)
        self.assertIn("8,364 tokens in", output)
        self.assertFalse(SeatingChart.objects.exists())

    def test_unsupported_file_type_errors(self):
        import tempfile

        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chart.svg")
            with open(path, "w") as f:
                f.write("<svg/>")
            with self.assertRaises(CommandError):
                call_command("parse_seating_chart", path, "--venue", str(self.venue.pk), stdout=StringIO())

    # -- target resolution: --org / --venue-name (onboarding path) ---------

    def _run(self, *args, expect_error=None):
        import tempfile
        from io import StringIO
        from unittest import mock

        from venues import chart_parsing

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_png(tmp)
            buf = StringIO()
            with mock.patch.object(chart_parsing, "_get_client", return_value=self._patched_client()):
                if expect_error is not None:
                    with self.assertRaisesMessage(CommandError, expect_error):
                        call_command("parse_seating_chart", path, *args, stdout=buf)
                else:
                    call_command("parse_seating_chart", path, *args, stdout=buf)
        return buf.getvalue()

    def test_org_with_venue_name_creates_the_venue(self):
        output = self._run("--org", "roxy", "--venue-name", "Second Space")
        self.assertIn("Created venue 'Second Space'", output)
        venue = Venue.objects.get(organization=self.org, name="Second Space")
        self.assertTrue(SeatingChart.objects.filter(venue=venue, name="Main house").exists())

    def test_org_with_existing_venue_name_reuses_it(self):
        self._run("--org", "roxy", "--venue-name", "Main Stage")
        self.assertEqual(Venue.objects.filter(organization=self.org).count(), 1)
        self.assertTrue(SeatingChart.objects.filter(venue=self.venue).exists())

    def test_org_alone_uses_the_sole_venue(self):
        self._run("--org", "roxy")
        self.assertTrue(SeatingChart.objects.filter(venue=self.venue).exists())

    def test_org_alone_errors_when_ambiguous_or_empty(self):
        Venue.objects.create(organization=self.org, name="Cabaret Room")
        self._run("--org", "roxy", expect_error="2 venues")

        Venue.objects.filter(organization=self.org).delete()
        self._run("--org", "roxy", expect_error="no venues yet")

    def test_unknown_org_and_conflicting_targets_error(self):
        self._run("--org", "nope", expect_error="No organization with subdomain 'nope'")
        self._run("--org", "roxy", "--venue", str(self.venue.pk), expect_error="not both")
        self._run("--venue-name", "X", expect_error="--venue-name only makes sense")

    def test_build_without_any_target_errors_before_parsing(self):
        self._run(expect_error="only --dry-run can run without")

    def test_dry_run_needs_no_target(self):
        output = self._run("--dry-run")
        self.assertIn('"chart_name": "Main house"', output)
        self.assertFalse(SeatingChart.objects.exists())

    def test_no_verify_runs_a_single_pass(self):
        import tempfile
        from io import StringIO
        from unittest import mock

        from venues import chart_parsing

        client = self._patched_client()
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_png(tmp)
            with mock.patch.object(chart_parsing, "_get_client", return_value=client):
                call_command(
                    "parse_seating_chart", path, "--org", "roxy", "--no-verify", stdout=StringIO()
                )
        self.assertEqual(client.messages.create.call_count, 1)
