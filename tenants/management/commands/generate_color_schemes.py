"""Report what the WCAG color generator (tenants.color_generator) would do to
the current catalog: which schemes' neutral/text colors it shifts, and which
can't reach AA (light-primary schemes where light text over the primary fill is
impossible).

Evaluation tool only -- the generator is NOT yet applied to the shipped
BUILTIN_SCHEMES (the contrast contract is being decided), so this command
writes nothing. `--check` exits non-zero if any scheme fails AA.
"""

from django.core.management.base import BaseCommand

from tenants.color_schemes import BUILTIN_SCHEMES
from tenants.color_generator import scheme_report


class Command(BaseCommand):
    help = "Report the WCAG generator's shifts + AA shortfalls on the source palette."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit non-zero if any scheme can't reach WCAG AA.",
        )

    def handle(self, *args, **options):
        report = scheme_report(BUILTIN_SCHEMES)
        shifted = [r for r in report if r["changes"]]
        failed = [r for r in report if r["warnings"]]

        for r in report:
            if not r["changes"] and not r["warnings"]:
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f"{r['name']} ({r['slug']})"))
            for role, before, after in r["changes"]:
                self.stdout.write(f"  {role}: {before} -> {after}")
            for warning in r["warnings"]:
                self.stdout.write(self.style.WARNING(f"  ! {warning}"))

        self.stdout.write("")
        self.stdout.write(
            f"{len(shifted)} scheme(s) nudged, {len(failed)} with an AA shortfall, "
            f"of {len(report)} total."
        )
        if options["check"] and failed:
            self.stderr.write(self.style.ERROR("AA shortfalls present (see above)."))
            raise SystemExit(1)
