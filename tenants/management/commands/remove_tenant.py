"""Deactivate (or, with --purge, delete) the Organization for a tenant
subdomain. This is the DB half of tenant offboarding — the infra half (nginx
vhost removal, and the certbot/DNS cleanup it prints reminders for) is done by
`bin/boxoffice remove-tenant <sub>`.

    venv/bin/python manage.py remove_tenant roxy            # sets is_active=False
    venv/bin/python manage.py remove_tenant roxy --purge     # deletes the row (destructive)

Default is non-destructive: the org and all its tenant-scoped data (events,
orders, tickets, ...) are left in the database with is_active=False, so the
tenant 404s (TenantMiddleware) but nothing is lost and re-activating is a
single field flip in /admin. --purge cascades (Organization FKs are
on_delete=CASCADE) and cannot be undone.
"""

from django.core.management.base import BaseCommand, CommandError

from tenants.models import Organization


class Command(BaseCommand):
    help = "Deactivate (default) or delete (--purge) the Organization for a tenant subdomain."

    def add_arguments(self, parser):
        parser.add_argument("subdomain", help="Subdomain label of the tenant to remove.")
        parser.add_argument(
            "--purge",
            action="store_true",
            help="Delete the Organization (and, via cascade, ALL its data) instead of "
            "just deactivating it. Destructive and irreversible.",
        )

    def handle(self, *args, **options):
        subdomain = options["subdomain"].strip().lower()
        try:
            org = Organization.objects.get(subdomain=subdomain)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"No organization with subdomain {subdomain!r}.") from exc

        if options["purge"]:
            name = org.name
            org.delete()
            self.stdout.write(
                self.style.WARNING(f"Deleted organization {name!r} (subdomain={subdomain}) and all its data.")
            )
        else:
            if not org.is_active:
                self.stdout.write(f"Organization {org.name!r} (subdomain={subdomain}) is already inactive.")
            else:
                org.is_active = False
                org.save(update_fields=["is_active"])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Deactivated organization {org.name!r} (subdomain={subdomain}). "
                        "It will now 404 for visitors; re-activate in /admin or re-run with "
                        "provision_tenant if needed."
                    )
                )
