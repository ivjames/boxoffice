"""`manage.py import_seating_chart <file> --venue <id>` -- rebuilds a
SeatingChart on the given Venue from JSON produced by export_seating_chart.
See venues/chart_io.py's import_chart_data docstring for exactly what
`--replace` does and doesn't allow.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from venues.chart_io import ChartImportError, import_chart_data
from venues.models import Venue


class Command(BaseCommand):
    help = (
        "Import a SeatingChart (sections/rows/seats/layout) from a JSON file produced by "
        "export_seating_chart."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to a JSON file from export_seating_chart.")
        parser.add_argument("--venue", type=int, required=True, help="Target Venue id.")
        parser.add_argument(
            "--name", help="Override the chart name from the file's \"chart\".\"name\"."
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "If a chart with this name already exists on the venue, delete its sections/"
                "seats and rebuild them from the file. Refuses if any of its seats have a live "
                "ticket issued."
            ),
        )

    def handle(self, *args, **options):
        try:
            venue = Venue.objects.get(pk=options["venue"])
        except Venue.DoesNotExist:
            raise CommandError(f"No Venue with id={options['venue']}.")

        try:
            with open(options["file"]) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Couldn't read {options['file']}: {exc}")

        try:
            chart = import_chart_data(venue, data, name=options.get("name"), replace=options["replace"])
        except ChartImportError as exc:
            raise CommandError(str(exc))

        section_count = chart.sections.count()
        seat_count = sum(section.seats.count() for section in chart.sections.all())
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported chart {chart.name!r} (id={chart.pk}) onto {venue} — "
                f"{section_count} section(s), {seat_count} seat(s)."
            )
        )
