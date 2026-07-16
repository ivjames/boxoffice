"""Re-sync the built-in presets after the generator learned to derive a
harmonious (analogous) feature accent from each scheme's primary
(tenants.color_generator.harmonize_accent). The feature_accent role moves on
most schemes -- and the two neutral/text roles may re-settle against it -- while
primary / secondary / dark_accent are unchanged. Same idempotent upsert (+
prune) as the earlier seeds.

Presets only: a tenant that already applied a scheme keeps its snapshot colors
(applying copies colors onto the org; editing a scheme never silently re-themes
a tenant -- see Organization.apply_color_scheme). Custom (org-owned) schemes are
untouched.
"""

from django.db import migrations

from tenants.color_schemes import sync_presets


def resync(apps, schema_editor):
    sync_presets(apps.get_model("tenants", "ColorScheme"))


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0013_apply_wcag_neutrals"),
    ]

    operations = [
        migrations.RunPython(resync, migrations.RunPython.noop),
    ]
