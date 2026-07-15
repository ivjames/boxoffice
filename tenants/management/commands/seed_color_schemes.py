"""Re-sync the built-in ColorScheme preset catalog from
tenants.color_schemes.BUILTIN_SCHEMES.

The tenants migration seeds these on deploy; this command re-runs the same
idempotent upsert so a preset can be added/tweaked in BUILTIN_SCHEMES and
pushed live without a schema migration. Only touches presets (organization
NULL, is_preset True) -- tenants' own saved schemes are never affected.
"""

from django.core.management.base import BaseCommand

from tenants.color_schemes import sync_presets
from tenants.models import ColorScheme


class Command(BaseCommand):
    help = "Sync the built-in color-scheme presets to match BUILTIN_SCHEMES."

    def handle(self, *args, **options):
        created, updated, deleted = sync_presets(ColorScheme)
        self.stdout.write(
            self.style.SUCCESS(
                f"Presets synced: {created} created, {updated} updated, {deleted} removed."
            )
        )
