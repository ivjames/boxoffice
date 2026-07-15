"""`manage.py parse_seating_chart <file> [--venue <id> | --org <subdomain>]
[--dry-run]` -- sends an image or PDF of a seating chart to the Claude API
(venues/chart_parsing.py) and builds a real, editor-ready SeatingChart from
the parsed section params. The CLI counterpart of the dashboard's "Import
from image/PDF" upload; same conventions as import_seating_chart (--name,
--replace with its live-ticket guard).

Target selection -- this command exists for onboarding, when the venue
often doesn't exist yet, so it can create one:

- `--venue <id>`: an existing Venue, same as import_seating_chart.
- `--org <subdomain> --venue-name "Main Stage"`: get-or-create that venue
  on the organization, then build the chart on it.
- `--org <subdomain>` alone: use the org's only venue (errors with a
  listing if it has zero or several).
- `--dry-run` needs no target at all -- it parses and prints, writing
  nothing.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from tenants.models import Organization
from venues.chart_parsing import (
    ChartParsingError,
    build_chart_from_spec,
    describe_usage,
    media_type_for_upload,
    parse_chart_file,
)
from venues.models import Venue


class Command(BaseCommand):
    help = (
        "Parse an image (PNG/JPEG/GIF/WebP) or PDF of a seating chart with the Claude API "
        "and build a SeatingChart (sections + generated seats) from it. Target an existing "
        "venue (--venue) or an organization (--org, creating the venue via --venue-name). "
        "Requires ANTHROPIC_API_KEY."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to the chart image or PDF.")
        parser.add_argument("--venue", type=int, help="Target Venue id (alternative: --org).")
        parser.add_argument(
            "--org",
            help=(
                "Organization subdomain to build the chart under -- alternative to --venue. "
                "Uses the org's only venue, or the one named by --venue-name (created on the "
                "org if it doesn't exist yet)."
            ),
        )
        parser.add_argument(
            "--venue-name",
            help=(
                "With --org: the venue to use; created on the organization if no venue with "
                "this name exists yet."
            ),
        )
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
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Parse only: print the extracted chart spec as JSON (plus token usage) and "
                "create nothing -- no target venue needed. Useful for evaluating the vision "
                "step against a known chart."
            ),
        )

    def _resolve_venue(self, options):
        """The Venue to build on, per the module docstring's target rules --
        or None when no target was given (allowed only with --dry-run;
        handle() enforces that). Resolved BEFORE the API call so a bad
        target fails fast instead of after spending tokens."""
        venue_id, subdomain = options.get("venue"), options.get("org")
        venue_name = options.get("venue_name")
        if venue_id and subdomain:
            raise CommandError("Pass either --venue or --org, not both.")
        if venue_name and not subdomain:
            raise CommandError("--venue-name only makes sense together with --org.")

        if venue_id:
            try:
                return Venue.objects.get(pk=venue_id)
            except Venue.DoesNotExist:
                raise CommandError(f"No Venue with id={venue_id}.")

        if subdomain:
            try:
                org = Organization.objects.get(subdomain=subdomain)
            except Organization.DoesNotExist:
                raise CommandError(f"No organization with subdomain {subdomain!r}.")
            if venue_name:
                venue, created = Venue.objects.get_or_create(
                    organization=org, name=venue_name
                )
                if created:
                    self.stdout.write(
                        f"Created venue {venue.name!r} (id={venue.pk}) on {org.name}."
                    )
                return venue
            venues = list(Venue.objects.filter(organization=org))
            if len(venues) == 1:
                return venues[0]
            if not venues:
                raise CommandError(
                    f"{org.name} has no venues yet -- pass --venue-name to create one."
                )
            listing = ", ".join(f"{v.pk}: {v.name}" for v in venues)
            raise CommandError(
                f"{org.name} has {len(venues)} venues ({listing}) -- pick one with "
                "--venue or --venue-name."
            )

        return None

    def handle(self, *args, **options):
        venue = self._resolve_venue(options)
        if venue is None and not options["dry_run"]:
            raise CommandError(
                "Pass --venue <id> or --org <subdomain> (only --dry-run can run without "
                "a target venue)."
            )

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
            if options["dry_run"]:
                printable = {key: value for key, value in spec.items() if key != "usage"}
                self.stdout.write(json.dumps(printable, indent=2))
                usage_line = describe_usage(spec.get("usage"))
                if usage_line:
                    self.stdout.write(f"API usage — {usage_line}.")
                return
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
        usage_line = describe_usage(spec.get("usage"))
        if usage_line:
            self.stdout.write(f"API usage — {usage_line}.")
