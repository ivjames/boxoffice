"""Re-sync the built-in ColorScheme presets to The Roxy Theater's 24-scheme
palette spec (tenants.color_schemes.BUILTIN_SCHEMES). Upserts the new set and
prunes the earlier starter presets whose slugs are gone -- via the same
idempotent sync_presets used by the `seed_color_schemes` command. Custom
(org-owned) schemes are untouched.
"""

from django.db import migrations

from tenants.color_schemes import sync_presets


def resync(apps, schema_editor):
    sync_presets(apps.get_model("tenants", "ColorScheme"))


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0008_organization_body_font_organization_heading_font"),
    ]

    # Data-only re-sync; the earlier seed (0007) is the reverse baseline, so
    # this migration's own reverse is a no-op (down-migrating just leaves the
    # current preset set in place rather than trying to reconstruct 0007's).
    operations = [
        migrations.RunPython(resync, migrations.RunPython.noop),
    ]
