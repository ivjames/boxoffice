"""Seed the built-in ColorScheme preset catalog (organization NULL,
is_preset True) from tenants.color_schemes.BUILTIN_SCHEMES.

Idempotent by (organization NULL, slug): re-running updates colors/ordering
in place rather than duplicating, so this and the `seed_color_schemes`
management command that shares the logic can both be run safely any time.
"""

from django.db import migrations

from tenants.color_schemes import BUILTIN_SCHEMES


def seed_presets(apps, schema_editor):
    ColorScheme = apps.get_model("tenants", "ColorScheme")
    for index, (slug, name, roles) in enumerate(BUILTIN_SCHEMES):
        ColorScheme.objects.update_or_create(
            organization=None,
            slug=slug,
            defaults={
                "name": name,
                "is_preset": True,
                "ordering": index,
                **roles,
            },
        )


def unseed_presets(apps, schema_editor):
    ColorScheme = apps.get_model("tenants", "ColorScheme")
    slugs = [slug for slug, _name, _roles in BUILTIN_SCHEMES]
    ColorScheme.objects.filter(organization=None, slug__in=slugs).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0006_organization_dark_accent_color_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_presets, unseed_presets),
    ]
