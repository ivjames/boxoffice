"""`manage.py export_seating_chart <chart-id>` -- dumps a SeatingChart
(sections -> rows -> seats, plus layout/numbering params) to JSON. See
venues/chart_io.py's export_chart_data docstring for the exact shape, and
import_seating_chart for the round-trip counterpart.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from venues.chart_io import export_chart_data
from venues.models import SeatingChart


class Command(BaseCommand):
    help = "Export a SeatingChart (sections/rows/seats/layout) to JSON."

    def add_arguments(self, parser):
        parser.add_argument("chart_id", type=int, help="SeatingChart id to export.")
        parser.add_argument(
            "--output", "-o", help="Write to this file instead of stdout."
        )

    def handle(self, *args, **options):
        try:
            chart = SeatingChart.objects.select_related("venue").get(pk=options["chart_id"])
        except SeatingChart.DoesNotExist:
            raise CommandError(f"No SeatingChart with id={options['chart_id']}.")

        data = export_chart_data(chart)
        text = json.dumps(data, indent=2)

        output = options.get("output")
        if output:
            with open(output, "w") as f:
                f.write(text)
            self.stdout.write(
                self.style.SUCCESS(f"Exported chart {chart.name!r} (id={chart.pk}) to {output}")
            )
        else:
            self.stdout.write(text)
