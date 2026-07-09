"""Create (or fetch) the Organization row for a new tenant subdomain. This is
the DB half of no-wildcard onboarding — the infra half (DNS + nginx vhost +
certbot) is done by `bin/boxoffice add-tenant <sub>`, which shells out to this
command for the DB step and then runs the lab980 provisioning tooling for the
rest. Deliberately does NOT touch Stripe keys or branding — those are set
later in Django admin (see docs/DEPLOY.md).

    venv/bin/python manage.py provision_tenant roxy --name "The Roxy Theater"

Idempotent: get_or_create on `subdomain`. Re-running with a different --name
does NOT rename an existing org (onboarding is a create-once operation; use
the admin to rename), it just reports that the org already exists.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from tenants.models import Organization


class Command(BaseCommand):
    help = "Create (or fetch) the Organization for a tenant subdomain. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "subdomain",
            help="Subdomain label for the tenant, e.g. 'roxy' for roxy.lab980.com.",
        )
        parser.add_argument(
            "--name",
            default=None,
            help="Display name for the theater (default: derived from the subdomain).",
        )
        parser.add_argument(
            "--contact-email",
            default=None,
            help="Contact email for the org (default: boxoffice@<subdomain>.<BASE_DOMAIN>).",
        )

    def handle(self, *args, **options):
        subdomain = options["subdomain"].strip().lower()
        if not subdomain or subdomain != slugify(subdomain):
            raise CommandError(
                f"{subdomain!r} is not a valid subdomain label "
                "(lowercase letters, digits, hyphens only)."
            )
        if subdomain in settings.RESERVED_SUBDOMAINS:
            raise CommandError(
                f"{subdomain!r} is a reserved subdomain ({sorted(settings.RESERVED_SUBDOMAINS)}); "
                "choose a different one for the tenant."
            )

        name = options["name"] or subdomain.replace("-", " ").title()
        contact_email = options["contact_email"] or f"boxoffice@{subdomain}.{settings.BASE_DOMAIN}"

        org, created = Organization.objects.get_or_create(
            subdomain=subdomain,
            defaults={
                "name": name,
                "slug": subdomain,
                "contact_email": contact_email,
            },
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Created organization {org.name!r} (subdomain={org.subdomain}).")
            )
        else:
            self.stdout.write(
                f"Organization already exists: {org.name!r} (subdomain={org.subdomain}, "
                f"is_active={org.is_active}) — nothing to do."
            )

        self.stdout.write(
            "Next: set Stripe keys and branding for this org in /admin (Organization "
            f"is_active={org.is_active})."
        )
        return f"{org.subdomain}\t{'created' if created else 'existing'}"
