"""Provision infrastructure (DNS + nginx vhost + TLS) for tenants queued from
the Django admin (Organization.infra_status == PENDING).

This is the privileged half of admin-driven tenant onboarding. The admin
action only flips a flag (a plain DB write — the web process has no business
running certbot); THIS command, run as root by cron every minute (mirroring
the Hold sweeper — see deploy/boxoffice-provision.cron), does the actual
DNS/nginx/TLS work and writes the outcome back to the row.

It doesn't reimplement any of that: it shells out to `bin/boxoffice add-tenant
<sub> --infra-only`, the same idempotent doctl/nginx/certbot flow the CLI
onboarding uses (the Organization row already exists — the admin created it —
so only the infra half runs). Safe to run every minute: it's a no-op when
nothing is PENDING, each row is claimed atomically so overlapping ticks can't
double-provision, and add-tenant leaves any existing DNS/vhost/cert as-is.

Provisions at most ONE tenant per run. certbot and DNS are slow and rate-
limited, so a batch queued together (e.g. several tenants selected in the
admin at once) drains one-per-minute across successive ticks rather than
firing several certbot runs in a single tick. Reserved-subdomain rows are
rejected without shelling out and don't consume the run's single slot.

Must run under prod settings (DJANGO_SETTINGS_MODULE=config.settings.prod) so
it reads the same DB the admin writes to, and as root so certbot/nginx/doctl
can act. Both are set in deploy/boxoffice-provision.cron.
"""

import os
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand

from tenants.models import Organization

_MSG_LIMIT = 4000  # keep infra_message readable in the admin


def _tail(text, limit=_MSG_LIMIT):
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


class Command(BaseCommand):
    help = (
        "Provision DNS/nginx/TLS for tenants queued in the admin "
        "(infra_status=pending). Run as root via cron; see "
        "deploy/boxoffice-provision.cron."
    )

    def handle(self, *args, **options):
        In = Organization.InfraStatus

        # FIFO by insertion (pk), so a batch queued together drains oldest-
        # first over successive ticks.
        pending = list(Organization.objects.filter(infra_status=In.PENDING).order_by("pk"))
        if not pending:
            return  # nothing queued — the common every-minute case

        # certbot/nginx/doctl all need root. Bail loudly rather than mark rows
        # PROVISIONED after a no-op run (add-tenant --infra-only just warns and
        # exits 0 when not root, which would otherwise look like success).
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            self.stderr.write(
                "provision_pending_tenants must run as root (certbot/nginx/doctl "
                f"need it); leaving {len(pending)} tenant(s) queued."
            )
            return

        boxoffice = settings.BASE_DIR / "bin" / "boxoffice"
        if not os.access(boxoffice, os.X_OK):
            self.stderr.write(f"operate CLI not executable at {boxoffice}; leaving queued.")
            return

        for org in pending:
            # Claim it atomically: only the tick that flips PENDING->PROVISIONING
            # (rowcount 1) proceeds, so two overlapping runs can't both provision.
            claimed = Organization.objects.filter(
                pk=org.pk, infra_status=In.PENDING
            ).update(infra_status=In.PROVISIONING)
            if not claimed:
                continue

            # Defensive: a reserved subdomain must never get a tenant vhost (it
            # belongs to the platform host). The admin doesn't block creating
            # such a row, so stop here rather than provision it.
            if org.subdomain in settings.RESERVED_SUBDOMAINS:
                self._finish(org, In.FAILED, f"'{org.subdomain}' is a reserved subdomain; refusing to provision.")
                continue

            self.stdout.write(f"provisioning {org.subdomain} ...")
            try:
                result = subprocess.run(
                    [str(boxoffice), "add-tenant", org.subdomain, "--infra-only"],
                    capture_output=True,
                    text=True,
                    timeout=600,  # certbot can wait on DNS; generous ceiling
                )
            except subprocess.TimeoutExpired:
                self._finish(org, In.FAILED, "add-tenant timed out after 600s (DNS still propagating?). Re-queue to retry.")
                return
            except Exception as exc:  # pragma: no cover - unexpected spawn failure
                self._finish(org, In.FAILED, f"failed to run add-tenant: {exc}")
                return

            output = _tail(f"{result.stdout}\n{result.stderr}")
            if result.returncode == 0:
                self._finish(org, In.PROVISIONED, output or "Provisioned.")
            else:
                self._finish(org, In.FAILED, output or f"add-tenant exited {result.returncode}.")

            # One real provisioning job per run: certbot/DNS are slow and
            # rate-limited, so we drain the queue one tenant per tick rather
            # than firing several add-tenant/certbot runs in a single minute.
            # A reserved-subdomain row above is cheap (no subprocess), so it
            # doesn't consume this run's slot -- we keep scanning for the
            # first tenant that actually needs provisioning.
            return

    def _finish(self, org, status, message):
        Organization.objects.filter(pk=org.pk).update(
            infra_status=status, infra_message=message
        )
        self.stdout.write(f"  {org.subdomain}: {status}")
