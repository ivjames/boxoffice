from django.db import migrations, models


class Migration(migrations.Migration):
    """Cosmetic help_text refresh only (domain migration lab980.com ->
    boxo.show); no schema change. See DEPLOY.md "Migrating ... to boxo.show"."""

    dependencies = [
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="subdomain",
            field=models.SlugField(
                max_length=63,
                unique=True,
                help_text="The subdomain this tenant is served on, e.g. 'roxy' for roxy.boxo.show.",
            ),
        ),
    ]
