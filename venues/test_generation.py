"""Tests for venues/generation.py: row-label schemes (skip I/O by default),
each numbering scheme, ragged rows, accessible flags, x/y placement, and the
destructive-regeneration guardrails (refuse w/o replace, refuse outright if
live tickets exist)."""

from django.test import TestCase

from events.models import Event, Performance
from orders.models import Order, Ticket
from venues.generation import SeatGenerationError, generate_row_labels, generate_seat_numbers, generate_seats
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org


class RowLabelTests(TestCase):
    def test_skip_io_default(self):
        labels = generate_row_labels(26, Section.RowLabelScheme.SKIP_IO)
        self.assertNotIn("I", labels)
        self.assertNotIn("O", labels)
        self.assertEqual(labels[:8], ["A", "B", "C", "D", "E", "F", "G", "H"])
        # H -> J (I skipped).
        self.assertEqual(labels[8], "J")

    def test_skip_io_doubles_after_24_letters(self):
        labels = generate_row_labels(26, Section.RowLabelScheme.SKIP_IO)
        # 24 usable letters (26 - I - O) before doubling starts.
        self.assertEqual(len(labels), 26)
        self.assertEqual(labels[23], "Z")  # last single letter (A..H,J..N,P..Z is 24 letters)
        self.assertEqual(labels[24], "AA")
        self.assertEqual(labels[25], "BB")

    def test_all_letters_scheme_includes_i_and_o(self):
        labels = generate_row_labels(26, Section.RowLabelScheme.ALL_LETTERS)
        self.assertIn("I", labels)
        self.assertIn("O", labels)
        self.assertEqual(labels[26 - 1], "Z")

    def test_short_section_never_doubles(self):
        labels = generate_row_labels(5, Section.RowLabelScheme.SKIP_IO)
        self.assertEqual(labels, ["A", "B", "C", "D", "E"])


class SeatNumberTests(TestCase):
    def test_sequential(self):
        self.assertEqual(
            generate_seat_numbers(4, Section.NumberingScheme.SEQUENTIAL, row_index=0), [1, 2, 3, 4]
        )

    def test_odd_desc_left(self):
        # Highest odd number on the left, descending toward the aisle.
        self.assertEqual(
            generate_seat_numbers(4, Section.NumberingScheme.ODD_DESC_LEFT, row_index=0),
            [7, 5, 3, 1],
        )

    def test_even_asc_right(self):
        self.assertEqual(
            generate_seat_numbers(4, Section.NumberingScheme.EVEN_ASC_RIGHT, row_index=0),
            [2, 4, 6, 8],
        )

    def test_hundreds_keyed_by_row(self):
        self.assertEqual(
            generate_seat_numbers(3, Section.NumberingScheme.HUNDREDS, row_index=0), [101, 102, 103]
        )
        self.assertEqual(
            generate_seat_numbers(3, Section.NumberingScheme.HUNDREDS, row_index=1), [201, 202, 203]
        )


class GenerateSeatsTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")

    def make_section(self, **overrides):
        defaults = {
            "organization": self.org,
            "chart": self.chart,
            "name": "Orchestra",
        }
        defaults.update(overrides)
        return Section.objects.create(**defaults)

    def test_uniform_grid_row_labels_and_numbers(self):
        section = self.make_section()
        seats = generate_seats(section, [4, 4])
        self.assertEqual(len(seats), 8)
        row_a = [s for s in seats if s.row_label == "A"]
        row_b = [s for s in seats if s.row_label == "B"]
        self.assertEqual(sorted(int(s.number) for s in row_a), [1, 2, 3, 4])
        self.assertEqual(sorted(int(s.number) for s in row_b), [1, 2, 3, 4])

    def test_ragged_rows(self):
        section = self.make_section()
        seats = generate_seats(section, [10, 10, 8, 12])
        counts = {}
        for seat in seats:
            counts.setdefault(seat.row_label, 0)
            counts[seat.row_label] += 1
        self.assertEqual(counts, {"A": 10, "B": 10, "C": 8, "D": 12})

    def test_x_y_are_persisted_and_left_to_right(self):
        section = self.make_section(origin_x=0, origin_y=0, seat_pitch=2.0, row_pitch=3.0)
        seats = generate_seats(section, [3])
        seats.sort(key=lambda s: s.number)
        xs = [s.x for s in seats]
        self.assertEqual(xs, [0.0, 2.0, 4.0])
        self.assertTrue(all(s.y == 0.0 for s in seats))
        # Persisted, not just in-memory -- refetch from the DB.
        refetched = list(Seat.objects.filter(section=section).order_by("number"))
        self.assertEqual([s.x for s in refetched], xs)

    def test_origin_offsets_the_whole_grid(self):
        section = self.make_section(origin_x=100.0, origin_y=50.0)
        seats = generate_seats(section, [2])
        self.assertTrue(all(s.x >= 100.0 for s in seats))
        self.assertTrue(all(s.y == 50.0 for s in seats))

    def test_accessible_flags_by_row_and_position(self):
        section = self.make_section()
        # Row 0 (A), left-to-right positions 1 and 2 are accessible.
        seats = generate_seats(section, [4, 4], accessible={0: {1, 2}})
        row_a = sorted((s for s in seats if s.row_label == "A"), key=lambda s: s.x)
        row_b = [s for s in seats if s.row_label == "B"]
        self.assertEqual([s.is_accessible for s in row_a], [True, True, False, False])
        self.assertTrue(all(not s.is_accessible for s in row_b))

    def test_odd_desc_left_scheme_end_to_end(self):
        section = self.make_section(numbering_scheme=Section.NumberingScheme.ODD_DESC_LEFT)
        seats = generate_seats(section, [4])
        seats.sort(key=lambda s: s.x)
        self.assertEqual([s.number for s in seats], ["7", "5", "3", "1"])

    def test_refuses_to_regenerate_without_replace(self):
        section = self.make_section()
        generate_seats(section, [2])
        with self.assertRaises(SeatGenerationError):
            generate_seats(section, [3])
        self.assertEqual(Seat.objects.filter(section=section).count(), 2)

    def test_replace_deletes_and_recreates(self):
        section = self.make_section()
        generate_seats(section, [2])
        seats = generate_seats(section, [5], replace=True)
        self.assertEqual(len(seats), 5)
        self.assertEqual(Seat.objects.filter(section=section).count(), 5)

    def test_refuses_even_with_replace_if_live_ticket_exists(self):
        section = self.make_section()
        generate_seats(section, [2])
        seat = section.seats.first()
        event = Event.objects.create(organization=self.org, title="Show", slug="show")
        venue = section.chart.venue
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=venue,
            starts_at="2030-01-01T19:00:00Z",
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        order = Order.objects.create(
            organization=self.org, performance=performance, buyer_email="x@example.com", total="10.00"
        )
        Ticket.objects.create(organization=self.org, order=order, performance=performance, seat=seat)

        with self.assertRaises(SeatGenerationError):
            generate_seats(section, [3], replace=True)
        self.assertEqual(Seat.objects.filter(section=section).count(), 2)

    def test_void_ticket_does_not_block_regeneration(self):
        section = self.make_section()
        generate_seats(section, [2])
        seat = section.seats.first()
        event = Event.objects.create(organization=self.org, title="Show", slug="show2")
        venue = section.chart.venue
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=venue,
            starts_at="2030-01-01T19:00:00Z",
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        order = Order.objects.create(
            organization=self.org, performance=performance, buyer_email="x@example.com", total="10.00"
        )
        Ticket.objects.create(
            organization=self.org,
            order=order,
            performance=performance,
            seat=seat,
            status=Ticket.Status.VOID,
        )

        seats = generate_seats(section, [4], replace=True)
        self.assertEqual(len(seats), 4)
