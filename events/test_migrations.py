"""Tests the 0005 data migration that backfills Performance.seating_chart on
every pre-existing row to whatever orders.services.get_seating_chart()
already resolved for it before the FK existed (the venue's first
SeatingChart, by pk) -- see that migration's docstring. Uses Django's
migration executor directly (migrate back to just before 0005, create data
against the HISTORICAL models, migrate forward, assert) since a normal
TestCase's DB is already fully migrated and has nothing to backfill.
"""

from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import TransactionTestCase


class BackfillSeatingChartMigrationTests(TransactionTestCase):
    # Actually running the migration graph backward/forward mid-test (real
    # DDL) needs TransactionTestCase, not TestCase -- TestCase wraps each
    # test in one outer transaction it rolls back, which doesn't play well
    # with a nested migrate() also managing its own transactions/DDL.
    migrate_from = [("events", "0004_performance_seating_chart_and_more")]
    migrate_to = [("events", "0005_backfill_performance_seating_chart")]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor

    def setUp(self):
        # Land on the state right after 0004 (FK exists, nothing backfilled
        # yet) so we can create historical rows through it.
        executor = self._migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def tearDown(self):
        # Always leave the DB at the latest migration state so the rest of
        # the suite (and TestCase's own teardown) sees the real, current
        # schema -- otherwise this test would permanently downgrade the
        # shared test DB for whatever runs after it.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_backfills_to_venues_first_chart(self):
        Organization = self.old_apps.get_model("tenants", "Organization")
        Venue = self.old_apps.get_model("venues", "Venue")
        SeatingChart = self.old_apps.get_model("venues", "SeatingChart")
        Event = self.old_apps.get_model("events", "Event")
        Performance = self.old_apps.get_model("events", "Performance")

        org = Organization.objects.create(
            name="Roxy", slug="roxy", subdomain="roxy", contact_email="box@roxy.example"
        )
        venue = Venue.objects.create(organization=org, name="Main Stage")
        chart_a = SeatingChart.objects.create(organization=org, venue=venue, name="A house")
        SeatingChart.objects.create(organization=org, venue=venue, name="Z house")
        event = Event.objects.create(organization=org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at="2030-01-01T19:00:00Z",
            seating_mode="RESERVED",
        )
        self.assertIsNone(performance.seating_chart_id)

        self._migrate(self.migrate_to)

        performance.refresh_from_db()
        # "First chart, by pk" -- chart_a was created first.
        self.assertEqual(performance.seating_chart_id, chart_a.pk)

    def test_venue_with_no_chart_left_null(self):
        Organization = self.old_apps.get_model("tenants", "Organization")
        Venue = self.old_apps.get_model("venues", "Venue")
        Event = self.old_apps.get_model("events", "Event")
        Performance = self.old_apps.get_model("events", "Performance")

        org = Organization.objects.create(
            name="Roxy", slug="roxy", subdomain="roxy", contact_email="box@roxy.example"
        )
        venue = Venue.objects.create(organization=org, name="Chartless Stage")
        event = Event.objects.create(organization=org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at="2030-01-01T19:00:00Z",
            seating_mode="GA",
        )

        self._migrate(self.migrate_to)

        performance.refresh_from_db()
        self.assertIsNone(performance.seating_chart_id)

    def test_already_set_seating_chart_is_left_alone(self):
        """Defensive: the migration only fills in NULLs. (Nothing sets it
        pre-0005 in practice, but this documents/guards the filter.)"""
        Organization = self.old_apps.get_model("tenants", "Organization")
        Venue = self.old_apps.get_model("venues", "Venue")
        SeatingChart = self.old_apps.get_model("venues", "SeatingChart")
        Event = self.old_apps.get_model("events", "Event")
        Performance = self.old_apps.get_model("events", "Performance")

        org = Organization.objects.create(
            name="Roxy", slug="roxy", subdomain="roxy", contact_email="box@roxy.example"
        )
        venue = Venue.objects.create(organization=org, name="Main Stage")
        chart_a = SeatingChart.objects.create(organization=org, venue=venue, name="A house")
        chart_b = SeatingChart.objects.create(organization=org, venue=venue, name="B house")
        event = Event.objects.create(organization=org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at="2030-01-01T19:00:00Z",
            seating_mode="RESERVED",
            seating_chart_id=chart_b.pk,
        )

        self._migrate(self.migrate_to)

        performance.refresh_from_db()
        self.assertEqual(performance.seating_chart_id, chart_b.pk)
