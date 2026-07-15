"""Re-sync the built-in presets after the Feature Accent column was revised to
muted/desaturated tones across all 24 schemes (tenants.color_schemes.
BUILTIN_SCHEMES). Same idempotent upsert (+ prune) as the earlier seeds; only
the metallic/Feature Accent values move. Custom (org-owned) schemes untouched.
"""

from django.db import migrations

from tenants.color_schemes import sync_presets


def resync(apps, schema_editor):
    sync_presets(apps.get_model("tenants", "ColorScheme"))


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0009_resync_roxy_color_schemes"),
    ]

    operations = [
        migrations.RunPython(resync, migrations.RunPython.noop),
    ]
