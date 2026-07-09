"""Sweep expired Holds so their inventory (GA quantity / seats) frees up for
other buyers immediately rather than waiting for the next mutation to filter
them out implicitly. Holds already stop counting against availability the
moment `expires_at` passes (every check in orders/services.py filters on
`expires_at__gt=now`) — this command just clears the now-inert rows out of
the table.

Run on a schedule, e.g. every minute, via cron or pm2:

    # crontab -e
    * * * * * cd /var/www/boxoffice && venv/bin/python manage.py release_expired_holds >> data/release_expired_holds.log 2>&1

    # or as a pm2 cron-restart job (pm2 ecosystem file):
    {
      "name": "boxoffice-hold-sweeper",
      "script": "venv/bin/python",
      "args": "manage.py release_expired_holds",
      "cron_restart": "* * * * *",
      "autorestart": false,
    }
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from orders.models import Hold


class Command(BaseCommand):
    help = "Delete Holds (and their HoldSeats, via cascade) whose expires_at has passed."

    def handle(self, *args, **options):
        now = timezone.now()
        expired = Hold.objects.filter(expires_at__lte=now)
        count = expired.count()
        expired.delete()
        self.stdout.write(self.style.SUCCESS(f"Released {count} expired hold(s)."))
