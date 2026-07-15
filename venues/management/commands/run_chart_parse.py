"""`manage.py run_chart_parse <job_id>` -- the worker half of the
dashboard's background chart parsing: executes one ChartParseJob (see
venues.chart_parsing.run_parse_job for claim/outcome semantics). Spawned as
a detached subprocess by chart_parsing.spawn_parse_job; runnable by hand to
retry a job stuck PENDING (a RUNNING/finished job won't be claimed twice).
"""

from django.core.management.base import BaseCommand, CommandError

from venues.chart_parsing import run_parse_job
from venues.models import ChartParseJob


class Command(BaseCommand):
    help = "Execute one background chart-parse job (internal worker for the dashboard upload flow)."

    def add_arguments(self, parser):
        parser.add_argument("job_id", type=int, help="ChartParseJob id to execute.")

    def handle(self, *args, **options):
        job_id = options["job_id"]
        if not ChartParseJob.objects.filter(pk=job_id).exists():
            raise CommandError(f"No ChartParseJob with id={job_id}.")
        job = run_parse_job(job_id)
        if job is None:
            self.stdout.write(f"Job {job_id} was not claimable (already running or finished).")
            return
        self.stdout.write(f"Job {job_id} finished: {job.status}" + (f" -- {job.error}" if job.error else ""))
