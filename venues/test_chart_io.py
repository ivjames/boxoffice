"""Tests for venues/chart_io.py: lossless JSON export/import round-trip,
tenant scoping (import always uses the target venue's own organization),
name-collision handling, and the live-ticket guard on --replace."""

from django.test import TestCase

from events.models import Event, Performance
from orders.models import Order, Ticket
from venues.chart_io import ChartImportError, export_chart_data, import_chart_data
from venues.generation import generate_seats
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org


class ChartIOTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Standard house"
        )
        self.orchestra = Section.objects.create(
            organization=self.org,
            chart=self.chart,
            name="Orchestra",
            tier="Orchestra",
            ordering=0,
            numbering_scheme=Section.NumberingScheme.ODD_DESC_LEFT,
            origin_x=1.0,
            seat_pitch=1.5,
        )
        self.balcony = Section.objects.create(
            organization=self.org, chart=self.chart, name="Balcony", ordering=1
        )
        generate_seats(self.orchestra, [4, 4], accessible={0: {1}})
        generate_seats(self.balcony, [6])

    def test_export_shape(self):
        data = export_chart_data(self.chart)
        self.assertEqual(data["chart"]["name"], "Standard house")
        self.assertEqual(len(data["sections"]), 2)
        orchestra_data = data["sections"][0]
        self.assertEqual(orchestra_data["name"], "Orchestra")
        self.assertEqual(orchestra_data["tier"], "Orchestra")
        self.assertEqual(orchestra_data["numbering_scheme"], "odd_desc_left")
        self.assertEqual(orchestra_data["layout"]["origin_x"], 1.0)
        self.assertEqual(orchestra_data["layout"]["seat_pitch"], 1.5)
        self.assertEqual(len(orchestra_data["rows"]), 2)
        row_a = orchestra_data["rows"][0]
        self.assertEqual(row_a["row_label"], "A")
        self.assertEqual(len(row_a["seats"]), 4)
        # odd_desc_left -> descending, and the leftmost (x smallest) seat in
        # row A was flagged accessible (position 1).
        self.assertEqual([s["number"] for s in row_a["seats"]], ["7", "5", "3", "1"])
        self.assertTrue(row_a["seats"][0]["accessible"])
        self.assertFalse(row_a["seats"][1]["accessible"])

    def test_round_trip_is_lossless(self):
        data = export_chart_data(self.chart)
        imported = import_chart_data(self.venue, data, name="Reimported house")
        reexported = export_chart_data(imported)

        # Same shape except the chart name we deliberately overrode.
        self.assertEqual(reexported["sections"], data["sections"])
        self.assertEqual(imported.sections.count(), self.chart.sections.count())
        self.assertEqual(
            Seat.objects.filter(section__chart=imported).count(),
            Seat.objects.filter(section__chart=self.chart).count(),
        )

    def test_round_trip_preserves_the_configurable_rotation_pivot(self):
        # Round 3 (docs/EDITOR.md #12): pivot_mode/pivot_x/pivot_y (Round
        # 2's configurable rotation pivot) must round-trip through export/
        # import like every other layout param -- previously dropped, so a
        # re-imported chart with a CUSTOM pivot silently reset to CENTER
        # and rotated around the wrong point.
        self.orchestra.pivot_mode = Section.PivotMode.CUSTOM
        self.orchestra.pivot_x = 3.5
        self.orchestra.pivot_y = -1.25
        self.orchestra.save(update_fields=["pivot_mode", "pivot_x", "pivot_y"])

        data = export_chart_data(self.chart)
        orchestra_data = next(s for s in data["sections"] if s["name"] == "Orchestra")
        self.assertEqual(orchestra_data["layout"]["pivot_mode"], "custom")
        self.assertEqual(orchestra_data["layout"]["pivot_x"], 3.5)
        self.assertEqual(orchestra_data["layout"]["pivot_y"], -1.25)

        imported = import_chart_data(self.venue, data, name="Reimported pivot house")
        imported_orchestra = imported.sections.get(name="Orchestra")
        self.assertEqual(imported_orchestra.pivot_mode, Section.PivotMode.CUSTOM)
        self.assertEqual(imported_orchestra.pivot_x, 3.5)
        self.assertEqual(imported_orchestra.pivot_y, -1.25)

        # And a section that never touched pivot_mode still imports the
        # CENTER default rather than blowing up on a missing key.
        imported_balcony = imported.sections.get(name="Balcony")
        self.assertEqual(imported_balcony.pivot_mode, Section.PivotMode.CENTER)

    def test_import_scopes_everything_to_the_target_venues_org(self):
        other_org = make_org("globe")
        other_venue = Venue.objects.create(organization=other_org, name="Globe Stage")
        data = export_chart_data(self.chart)

        imported = import_chart_data(other_venue, data)

        self.assertEqual(imported.organization_id, other_org.id)
        for section in imported.sections.all():
            self.assertEqual(section.organization_id, other_org.id)
            for seat in section.seats.all():
                self.assertEqual(seat.organization_id, other_org.id)
        # The original org's chart/seats are untouched.
        self.assertEqual(Seat.objects.filter(section__chart=self.chart).count(), 14)

    def test_import_refuses_name_collision_without_replace(self):
        data = export_chart_data(self.chart)
        with self.assertRaises(ChartImportError):
            import_chart_data(self.venue, data)  # same default name as self.chart

    def test_import_with_replace_rebuilds_in_place(self):
        data = export_chart_data(self.chart)
        # Mutate the JSON so we can tell it actually rebuilt.
        data["sections"] = data["sections"][:1]

        rebuilt = import_chart_data(self.venue, data, replace=True)

        self.assertEqual(rebuilt.pk, self.chart.pk)
        self.assertEqual(rebuilt.sections.count(), 1)

    def test_import_with_replace_refuses_if_live_ticket_exists(self):
        seat = self.orchestra.seats.first()
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=self.venue,
            starts_at="2030-01-01T19:00:00Z",
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        order = Order.objects.create(
            organization=self.org, performance=performance, buyer_email="x@example.com", total="10.00"
        )
        Ticket.objects.create(organization=self.org, order=order, performance=performance, seat=seat)

        data = export_chart_data(self.chart)
        with self.assertRaises(ChartImportError):
            import_chart_data(self.venue, data, replace=True)
        # Nothing was touched.
        self.assertEqual(Seat.objects.filter(section__chart=self.chart).count(), 14)
