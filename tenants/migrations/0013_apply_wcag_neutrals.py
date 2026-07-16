"""Re-sync the built-in presets after the WCAG generator (best-of-two per
surface, tenants.color_generator) was applied to BUILTIN_SCHEMES. Only the two
neutral/text roles moved (28 schemes nudged); the four brand roles are
unchanged. Same idempotent upsert as the earlier seeds. Custom (org-owned)
schemes are untouched.
"""

from django.db import migrations

from tenants.color_schemes import sync_presets


def resync(apps, schema_editor):
    sync_presets(apps.get_model("tenants", "ColorScheme"))


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0012_expand_to_36_schemes"),
    ]

    operations = [
        migrations.RunPython(resync, migrations.RunPython.noop),
    ]
