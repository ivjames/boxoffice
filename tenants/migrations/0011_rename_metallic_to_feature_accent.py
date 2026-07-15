"""Rename ColorScheme.metallic -> ColorScheme.feature_accent.

The role was labelled "Feature Accent" in the UI while the column/role key kept
the historical name `metallic`; this renames the column to match the function.
RenameField preserves the stored values, so the presets' Feature Accent colors
carry over untouched. The seed/re-sync migrations before this one write through
tenants.color_schemes.roles_for_model, which maps the current `feature_accent`
key back onto the `metallic` column while it still exists -- so a fresh migrate
runs clean end to end.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0010_update_feature_accent_colors"),
    ]

    operations = [
        migrations.RenameField(
            model_name="colorscheme",
            old_name="metallic",
            new_name="feature_accent",
        ),
    ]
