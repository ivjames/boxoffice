"""`manage.py parse_seating_chart <file> --venue <id>` -- sends an image or
PDF of a seating chart to the Claude API (venues/chart_parsing.py) and
builds a real, editor-ready SeatingChart on the given Venue from the parsed
section params. The CLI counterpart of the dashboard's "Import from
image/PDF" upload; same conventions as import_seating_chart (--name,
--replace with its live-ticket guard).
"""

from django.core.management.base import BaseCommand, CommandError

from venues.chart_parsing import (
    ChartParsingError,
    build_chart_from_spec,
    media_type_for_upload,
    parse_chart_file,
)
from venues.models import Venue


class Command(BaseCommand):
    help = (
        "Parse an image (PNG/JPEG/GIF/WebP) or PDF of a seating chart with the Claude API "
        "and build a SeatingChart (sections + generated seats) from it. Requires "
        "ANTHROPIC_API_KEY."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to the chart image or PDF.")
        parser.add_argument("--venue", type=int, required=True, help="Target Venue id.")
        parser.add_argument(
            "--name", help="Chart name to save under (default: the name the parser picks)."
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "If a chart with this name already exists on the venue, delete its sections/"
                "seats and rebuild them from the parse. Refuses if any of its seats have a "
                "live ticket issued."
            ),
        )

    def handle(self, *args, **options):
        try:
            venue = Venue.objects.get(pk=options["venue"])
        except Venue.DoesNotExist:
            raise CommandError(f"No Venue with id={options['venue']}.")

        path = options["file"]
        media_type = media_type_for_upload(path)
        if media_type is None:
            raise CommandError(f"{path}: not a supported file type (PNG, JPEG, GIF, WebP or PDF).")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise CommandError(f"Couldn't read {path}: {exc}")

        try:
            spec = parse_chart_file(data, media_type)
            chart = build_chart_from_spec(
                venue, spec, name=options.get("name"), replace=options["replace"]
            )
        except ChartParsingError as exc:
            raise CommandError(str(exc))

        section_count = chart.sections.count()
        seat_count = sum(section.seats.count() for section in chart.sections.all())
        self.stdout.write(
            self.style.SUCCESS(
                f"Parsed chart {chart.name!r} (id={chart.pk}) onto {venue} — "
                f"{section_count} section(s), {seat_count} seat(s). Review it in the "
                f"dashboard chart editor."
            )
        )
