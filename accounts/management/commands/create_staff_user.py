"""Bootstrap a staff user + Membership for a tenant. This is the operator
path for onboarding real staff (the storefront never lets a buyer self-
register a staff account) -- see also `create_demo_tenant --subdomain <sub>`,
which calls this same logic to seed one demo owner.

    venv/bin/python manage.py create_staff_user \\
        --email owner@roxy.example.com --password s3cret --org roxy --role owner

Idempotent-ish: if the User already exists, its password/name are left alone
unless --reset-password is given, and the Membership's role is updated
in-place rather than erroring on the (user, organization) unique constraint.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Membership
from tenants.models import Organization

User = get_user_model()


class Command(BaseCommand):
    help = "Create (or update) a staff User + Membership for one Organization."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--org", required=True, help="Organization subdomain.")
        parser.add_argument(
            "--role",
            required=True,
            choices=[choice[0] for choice in Membership.Role.choices],
        )
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="If the user already exists, overwrite their password with --password.",
        )

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(subdomain=options["org"])
        except Organization.DoesNotExist as exc:
            raise CommandError(f"No organization with subdomain {options['org']!r}.") from exc

        email = User.objects.normalize_email(options["email"])

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": options["first_name"],
                    "last_name": options["last_name"],
                },
            )
            if created:
                user.set_password(options["password"])
                user.save(update_fields=["password"])
            elif options["reset_password"]:
                user.set_password(options["password"])
                user.save(update_fields=["password"])

            membership, membership_created = Membership.objects.update_or_create(
                user=user, organization=organization, defaults={"role": options["role"]}
            )

        status = "created" if created else ("password reset" if options["reset_password"] else "already existed")
        self.stdout.write(
            self.style.SUCCESS(
                f"User {user.email} ({status}) is now '{membership.role}' at {organization.name} "
                f"({organization.subdomain})."
            )
        )
