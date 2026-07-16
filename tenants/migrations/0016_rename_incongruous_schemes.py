"""Re-sync presets after renaming two schemes whose names no longer matched
their harmonized accents: "Blush & Teal" -> "Blush & Clay" (blush-teal ->
blush-clay) and "Ivory & Sapphire" -> "Ivory & Garnet" (ivory-sapphire ->
ivory-garnet). The accent in each moved off teal/sapphire to a warm clay/garnet,
so the second color in the name was gone.

sync_presets upserts the renamed slugs and prunes the old ones (the prune is
scoped to organization=NULL presets). A tenant that had applied the old preset
keeps its snapshot colors -- applying copies colors onto the org, it doesn't
hold a live reference to the scheme.
"""

from django.db import migrations

from tenants.color_schemes import sync_presets


def resync(apps, schema_editor):
    sync_presets(apps.get_model("tenants", "ColorScheme"))


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0015_organization_page_tint"),
    ]

    operations = [
        migrations.RunPython(resync, migrations.RunPython.noop),
    ]
