"""Re-sync the built-in presets after the catalog grew from 24 to 36 schemes
(tenants.color_schemes.BUILTIN_SCHEMES). Same idempotent upsert (+ prune) as
the earlier seeds: the 12 new schemes are inserted, the existing 24 are updated
in place (values unchanged), and their ordering is refreshed to the new
spectrum order. Custom (org-owned) schemes are untouched.
"""

from django.db import migrations

from tenants.color_schemes import sync_presets


def resync(apps, schema_editor):
    sync_presets(apps.get_model("tenants", "ColorScheme"))


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0011_alter_organization_accent_color"),
    ]

    operations = [
        migrations.RunPython(resync, migrations.RunPython.noop),
    ]
