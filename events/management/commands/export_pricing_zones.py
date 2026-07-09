"""`manage.py export_pricing_zones <performance-id>` -- Phase D of the
seating-chart epic (docs/SEATING.md "D"): render a performance's
pricing-zone map to PNG or PDF and write it to a file. Thin CLI wrapper
around events.zone_export.render_zone_map -- see that module's docstring
for the renderer itself (Pillow for PNG, reportlab for PDF, no
browser/Chromium at runtime).
"""

from django.core.management.base import BaseCommand, CommandError

from events.models import Performance
from events.zone_export import ZoneExportError, render_zone_map


class Command(BaseCommand):
    help = "Export a performance's pricing-zone map to a PNG or PDF file."

    def add_arguments(self, parser):
        parser.add_argument("performance_id", type=int, help="Performance id to export.")
        parser.add_argument(
            "--format", choices=["png", "pdf"], default="png", help="Output format (default: png)."
        )
        parser.add_argument(
            "--size", choices=["letter", "legal"], default="letter", help="Paper size (default: letter)."
        )
        parser.add_argument(
            "--no-labels", action="store_true", help="Omit seat row/number labels."
        )
        parser.add_argument(
            "--no-legend", action="store_true", help="Omit the zone/price legend."
        )
        parser.add_argument(
            "--out", "-o", required=True, help="Path to write the rendered file to."
        )

    def handle(self, *args, **options):
        try:
            performance = Performance.objects.select_related("event", "venue").get(
                pk=options["performance_id"]
            )
        except Performance.DoesNotExist:
            raise CommandError(f"No Performance with id={options['performance_id']}.")

        fmt = options["format"]
        try:
            content = render_zone_map(
                performance,
                fmt=fmt,
                size=options["size"],
                labels=not options["no_labels"],
                legend=not options["no_legend"],
            )
        except ZoneExportError as exc:
            raise CommandError(str(exc))

        out_path = options["out"]
        with open(out_path, "wb") as f:
            f.write(content)

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {fmt.upper()} pricing-zone map for performance {performance.pk} "
                f"({performance.event.title}) to {out_path} ({len(content)} bytes)."
            )
        )
