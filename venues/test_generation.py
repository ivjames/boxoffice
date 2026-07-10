"""Tests for venues/generation.py: row-label schemes (skip I/O by default),
each numbering scheme, ragged rows, accessible flags, x/y placement, and the
destructive-regeneration guardrails (refuse w/o replace, refuse outright if
live tickets exist).

Phase B (docs/SEATING.md "B. Geometry + visual editor") adds GeometryTests
below: grid math is unchanged (asserted directly, not just "same as before"),
plus raked/diagonal (rotation + row_x_offset) and fanned (arc_radius) --
coordinates asserted within tolerance via assertAlmostEqual since trig is
involved."""

import math

from django.test import TestCase

from events.models import Event, Performance
from orders.models import Order, Ticket
from venues.generation import (
    SeatGenerationError,
    compute_row_counts,
    generate_row_labels,
    generate_seat_numbers,
    generate_seats,
    pivot_xy,
)
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


class GeometryTests(TestCase):
    """Phase B: grid/raked/fanned coordinate generation (venues.generation's
    _seat_xy dispatch). Uses generate_seats end-to-end (not the private
    helpers directly) so these also exercise the public contract staff code
    calls."""

    def setUp(self):
        self.org = make_org("roxy")
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")

    def make_section(self, **overrides):
        defaults = {"organization": self.org, "chart": self.chart, "name": "Orchestra"}
        defaults.update(overrides)
        return Section.objects.create(**defaults)

    def seats_by_row(self, section):
        seats = list(section.seats.order_by("row_label", "number"))
        by_row = {}
        for seat in seats:
            by_row.setdefault(seat.row_label, []).append(seat)
        return by_row

    # -- grid (rotation=0, row_x_offset=0, arc_radius=None) -- unchanged from
    # Phase A; asserted directly here (not just "matches old behavior").

    def test_grid_is_a_plain_rectangle(self):
        section = self.make_section(origin_x=0, origin_y=0, seat_pitch=2.0, row_pitch=3.0)
        generate_seats(section, [3, 3])
        by_row = self.seats_by_row(section)
        row_a = sorted(by_row["A"], key=lambda s: s.number)
        row_b = sorted(by_row["B"], key=lambda s: s.number)
        self.assertEqual([s.x for s in row_a], [0.0, 2.0, 4.0])
        self.assertEqual([s.y for s in row_a], [0.0, 0.0, 0.0])
        self.assertEqual([s.x for s in row_b], [0.0, 2.0, 4.0])
        self.assertEqual([s.y for s in row_b], [3.0, 3.0, 3.0])

    # -- raked/diagonal: rotation + per-row x_offset --------------------

    def test_raked_row_x_offset_staggers_rows_into_a_trapezoid(self):
        # No rotation -- row_x_offset alone should shift each row right by
        # a growing amount, independent of rotation.
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=1.0, row_x_offset=0.5, rotation=0.0
        )
        generate_seats(section, [3, 3, 3])
        by_row = self.seats_by_row(section)
        row_a = sorted(by_row["A"], key=lambda s: s.number)
        row_b = sorted(by_row["B"], key=lambda s: s.number)
        row_c = sorted(by_row["C"], key=lambda s: s.number)
        self.assertEqual([s.x for s in row_a], [0.0, 1.0, 2.0])
        self.assertEqual([s.x for s in row_b], [0.5, 1.5, 2.5])
        self.assertEqual([s.x for s in row_c], [1.0, 2.0, 3.0])
        self.assertEqual([s.y for s in row_a], [0.0, 0.0, 0.0])
        self.assertEqual([s.y for s in row_b], [1.0, 1.0, 1.0])
        self.assertEqual([s.y for s in row_c], [2.0, 2.0, 2.0])

    def test_raked_ragged_rows_form_an_angled_block_edge(self):
        # A side section: ragged (shrinking) row lengths + a growing
        # row_x_offset -- the "diagonal wall edge" from docs/SEATING.md.
        section = self.make_section(row_x_offset=1.0, seat_pitch=1.0, row_pitch=1.0)
        generate_seats(section, [5, 4, 3])
        by_row = self.seats_by_row(section)
        # Rightmost seat of each row (the aisle-side edge, ascending x)
        # traces a straight diagonal line despite ragged row lengths.
        rightmost = {
            label: max(s.x for s in seats) for label, seats in by_row.items()
        }
        self.assertAlmostEqual(rightmost["A"], 4.0)  # 0..4, row_x_offset=0
        self.assertAlmostEqual(rightmost["B"], 4.0)  # 0 + 1*1.0 .. 3 + 1.0
        self.assertAlmostEqual(rightmost["C"], 4.0)  # 0 + 2*1.0 .. 2 + 2.0
        # i.e. the wall edge (rightmost seat) is flush across all 3 rows --
        # exactly the ragged-row + growing-offset trapezoid shape.

    def test_raked_rotation_tilts_the_whole_staggered_block(self):
        # Pinned to ORIGIN pivot_mode -- this test is about the raked
        # rotation FORMULA (does a 90-degree turn swap the local axes),
        # which is easiest to state relative to local (0, 0); the Round-2
        # default-pivot behavior itself is covered separately below by
        # PivotModeTests.
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=1.0, row_x_offset=0.0, rotation=90.0,
            pivot_mode=Section.PivotMode.ORIGIN,
        )
        generate_seats(section, [1, 1])
        by_row = self.seats_by_row(section)
        seat_a = by_row["A"][0]
        seat_b = by_row["B"][0]
        # A 90-degree rotation swaps the local (x, y) axes: row B (local
        # y=1, x=0) should land near (-1, 0) rather than (0, 1).
        self.assertAlmostEqual(seat_a.x, 0.0, places=6)
        self.assertAlmostEqual(seat_a.y, 0.0, places=6)
        self.assertAlmostEqual(seat_b.x, -1.0, places=6)
        self.assertAlmostEqual(seat_b.y, 0.0, places=6)

    def test_raked_origin_offsets_the_whole_block(self):
        section = self.make_section(origin_x=50.0, origin_y=20.0, row_x_offset=1.0)
        generate_seats(section, [2, 2])
        for seat in section.seats.all():
            self.assertGreaterEqual(seat.x, 50.0)
            self.assertGreaterEqual(seat.y, 20.0)

    # -- alternating offset_mode -------------------------------------------

    def test_alternating_offset_staggers_only_every_other_row(self):
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=1.0, row_x_offset=0.5,
            offset_mode=Section.OffsetMode.ALTERNATING,
        )
        generate_seats(section, [3, 3, 3])
        by_row = self.seats_by_row(section)
        row_a = sorted(by_row["A"], key=lambda s: s.number)
        row_b = sorted(by_row["B"], key=lambda s: s.number)
        row_c = sorted(by_row["C"], key=lambda s: s.number)
        self.assertEqual([s.x for s in row_a], [0.0, 1.0, 2.0])
        self.assertEqual([s.x for s in row_b], [0.5, 1.5, 2.5])
        # Row C (row_index=2, even) is NOT offset in ALTERNATING mode --
        # unlike REPEATED, which would grow it to 1.0 (see
        # test_raked_row_x_offset_staggers_rows_into_a_trapezoid above).
        self.assertEqual([s.x for s in row_c], [0.0, 1.0, 2.0])

    def test_alt_row_seat_delta_adds_or_drops_seats_on_alternating_rows_only(self):
        self.assertEqual(
            compute_row_counts(
                rows=4, seats_per_row=5, offset_mode=Section.OffsetMode.ALTERNATING,
                alt_row_seat_delta=-2,
            ),
            [5, 3, 5, 3],
        )
        self.assertEqual(
            compute_row_counts(
                rows=4, seats_per_row=5, offset_mode=Section.OffsetMode.ALTERNATING,
                alt_row_seat_delta=2,
            ),
            [5, 7, 5, 7],
        )
        # REPEATED mode ignores alt_row_seat_delta entirely.
        self.assertEqual(
            compute_row_counts(
                rows=3, seats_per_row=5, offset_mode=Section.OffsetMode.REPEATED,
                alt_row_seat_delta=-2,
            ),
            [5, 5, 5],
        )

    def test_alt_row_seat_delta_floors_at_one_seat(self):
        self.assertEqual(
            compute_row_counts(
                rows=2, seats_per_row=1, offset_mode=Section.OffsetMode.ALTERNATING,
                alt_row_seat_delta=-5,
            ),
            [1, 1],
        )

    # -- fanned: arc_radius set, curving IN PLACE --------------------------
    #
    # BUG FIX (docs/EDITOR.md): arc_radius used to translate the whole
    # section away from its origin by `arc_radius` -- these tests assert
    # the fixed "in place" behavior (front-row-center always at the
    # section's origin; arc_radius only controls curvature).

    def test_fanned_row_curves_in_place_around_a_focal_point_behind_the_section(self):
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=5.0, arc_radius=10.0
        )
        generate_seats(section, [3])
        seats = sorted(section.seats.all(), key=lambda s: s.x)
        # Center seat (row midpoint) sits exactly at the section's origin --
        # NOT translated out by arc_radius like the old (buggy) formula.
        self.assertAlmostEqual(seats[1].x, 0.0, places=6)
        self.assertAlmostEqual(seats[1].y, 0.0, places=6)
        # Left/right seats are symmetric around x=0 and curve toward the
        # origin (smaller y) at the row's ends -- concave curvature.
        self.assertAlmostEqual(seats[0].x, -seats[2].x, places=6)
        self.assertAlmostEqual(seats[0].y, seats[2].y, places=6)
        self.assertLess(seats[0].y, seats[1].y)
        # Every seat sits `arc_radius` from the focal point BEHIND the
        # section (origin_y - arc_radius) -- not from the origin itself.
        focal_y = -10.0
        for seat in seats:
            self.assertAlmostEqual(math.hypot(seat.x, seat.y - focal_y), 10.0, places=6)
        angle_step = 1.0 / 10.0
        self.assertAlmostEqual(seats[0].x, 10.0 * math.sin(-angle_step), places=6)
        self.assertAlmostEqual(seats[2].x, 10.0 * math.sin(angle_step), places=6)

    def test_arc_radius_does_not_translate_the_section(self):
        # THE bug fix, stated directly: front-row-center sits at the
        # section's origin no matter how big arc_radius is.
        tight = self.make_section(
            name="Tight", origin_x=5.0, origin_y=5.0, seat_pitch=1.0, row_pitch=5.0, arc_radius=5.0
        )
        generate_seats(tight, [1])
        wide = self.make_section(
            name="Wide", origin_x=5.0, origin_y=5.0, seat_pitch=1.0, row_pitch=5.0, arc_radius=500.0
        )
        generate_seats(wide, [1])
        tight_seat = tight.seats.get()
        wide_seat = wide.seats.get()
        self.assertAlmostEqual(tight_seat.x, 5.0, places=6)
        self.assertAlmostEqual(tight_seat.y, 5.0, places=6)
        self.assertAlmostEqual(wide_seat.x, 5.0, places=6)
        self.assertAlmostEqual(wide_seat.y, 5.0, places=6)

    def test_fanned_rows_step_outward_by_row_pitch_in_place(self):
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=5.0, arc_radius=10.0
        )
        generate_seats(section, [1, 1, 1])
        by_row = self.seats_by_row(section)
        # Single-seat rows sit dead center (theta=0) -- rows step back by
        # row_pitch each, starting from the origin (row A = 0), regardless
        # of arc_radius.
        self.assertAlmostEqual(by_row["A"][0].y, 0.0, places=6)
        self.assertAlmostEqual(by_row["B"][0].y, 5.0, places=6)
        self.assertAlmostEqual(by_row["C"][0].y, 10.0, places=6)
        for label in ("A", "B", "C"):
            self.assertAlmostEqual(by_row[label][0].x, 0.0, places=6)

    def test_fanned_rotation_pivots_on_origin_same_as_raked(self):
        # Pinned to ORIGIN pivot_mode -- see the raked test's comment above;
        # the default CENTER pivot is covered by PivotModeTests.
        plain = self.make_section(
            name="Plain", origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=5.0, arc_radius=10.0,
            pivot_mode=Section.PivotMode.ORIGIN,
        )
        generate_seats(plain, [1, 1])
        plain_row_b = self.seats_by_row(plain)["B"][0]

        rotated = self.make_section(
            name="Rotated", origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=5.0,
            arc_radius=10.0, rotation=90.0, pivot_mode=Section.PivotMode.ORIGIN,
        )
        generate_seats(rotated, [1, 1])
        rotated_row_b = self.seats_by_row(rotated)["B"][0]

        self.assertAlmostEqual(plain_row_b.x, 0.0, places=6)
        self.assertAlmostEqual(plain_row_b.y, 5.0, places=6)
        # Same 90-degree pivot-on-origin transform as the raked case (see
        # test_raked_rotation_tilts_the_whole_staggered_block): local
        # (0, y) -> (-y, 0).
        self.assertAlmostEqual(rotated_row_b.x, -5.0, places=6)
        self.assertAlmostEqual(rotated_row_b.y, 0.0, places=6)

    def test_arc_radius_none_falls_back_to_grid_or_raked(self):
        section = self.make_section(arc_radius=None, row_x_offset=0.0, rotation=0.0)
        generate_seats(section, [2])
        seats = sorted(section.seats.all(), key=lambda s: s.x)
        self.assertEqual([s.x for s in seats], [0.0, 1.0])
        self.assertEqual([s.y for s in seats], [0.0, 0.0])


class SeatOverrideTests(TestCase):
    """docs/EDITOR.md's per-seat popover overrides: removed_ids/
    accessible_ids are (row_label, number) identities, tracked on the
    Section (not the Seat pk, which doesn't survive a regenerate) and
    re-applied on every generate_seats(replace=True) call."""

    def setUp(self):
        self.org = make_org("roxy")
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")

    def make_section(self, **overrides):
        defaults = {"organization": self.org, "chart": self.chart, "name": "Orchestra"}
        defaults.update(overrides)
        return Section.objects.create(**defaults)

    def test_removed_ids_skip_those_seats(self):
        section = self.make_section()
        seats = generate_seats(section, [3], removed_ids={("A", "2")})
        self.assertEqual(sorted(int(s.number) for s in seats), [1, 3])

    def test_accessible_ids_flag_by_identity(self):
        section = self.make_section()
        generate_seats(section, [3], accessible_ids={("A", "2")})
        self.assertTrue(section.seats.get(number="2").is_accessible)
        self.assertFalse(section.seats.get(number="1").is_accessible)

    def test_removed_ids_persist_across_regenerate_with_replace(self):
        section = self.make_section()
        generate_seats(section, [3])
        generate_seats(section, [3], removed_ids={("A", "2")}, replace=True)
        self.assertEqual(section.seats.count(), 2)
        self.assertFalse(section.seats.filter(number="2").exists())


class SharedFormulaContractTests(TestCase):
    """docs/EDITOR.md: 'add a test asserting the server geometry matches
    expected coordinates for representative params (grid, repeated-offset,
    alternating-offset, arc, tilt)'. These hand-computed expected values
    ARE the contract static/js/seat_geometry.js's matching functions must
    reproduce exactly -- see generation.py's module docstring. A drift
    between the two implementations can't be caught by pytest directly
    (there's no JS test runner in this stack), but it WILL show up here as
    soon as anyone changes one side's formula without the other, because
    both sides are meant to satisfy these same numbers."""

    def setUp(self):
        self.org = make_org("contract")
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.chart = SeatingChart.objects.create(organization=self.org, venue=venue, name="Standard")

    def make_section(self, **overrides):
        defaults = {"organization": self.org, "chart": self.chart, "name": "Contract"}
        defaults.update(overrides)
        return Section.objects.create(**defaults)

    def by_identity(self, seats):
        return {(s.row_label, s.number): (s.x, s.y) for s in seats}

    def test_grid(self):
        section = self.make_section(origin_x=10, origin_y=20, seat_pitch=2.0, row_pitch=3.0)
        by = self.by_identity(generate_seats(section, [2, 2]))
        self.assertEqual(by[("A", "1")], (10.0, 20.0))
        self.assertEqual(by[("A", "2")], (12.0, 20.0))
        self.assertEqual(by[("B", "1")], (10.0, 23.0))
        self.assertEqual(by[("B", "2")], (12.0, 23.0))

    def test_repeated_offset(self):
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=1.0, row_x_offset=0.5,
            offset_mode=Section.OffsetMode.REPEATED,
        )
        by = self.by_identity(generate_seats(section, [2, 2, 2]))
        self.assertEqual(by[("A", "1")], (0.0, 0.0))
        self.assertEqual(by[("B", "1")], (0.5, 1.0))
        self.assertEqual(by[("C", "1")], (1.0, 2.0))

    def test_alternating_offset(self):
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=1.0, row_x_offset=0.5,
            offset_mode=Section.OffsetMode.ALTERNATING, alt_row_seat_delta=1,
        )
        row_counts = compute_row_counts(
            rows=3, seats_per_row=2, offset_mode=section.offset_mode,
            alt_row_seat_delta=section.alt_row_seat_delta,
        )
        self.assertEqual(row_counts, [2, 3, 2])
        by = self.by_identity(generate_seats(section, row_counts))
        self.assertEqual(by[("A", "1")], (0.0, 0.0))
        self.assertEqual(by[("B", "1")], (0.5, 1.0))
        self.assertEqual(by[("B", "3")], (2.5, 1.0))
        self.assertEqual(by[("C", "1")], (0.0, 2.0))

    def test_arc(self):
        section = self.make_section(origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=5.0, arc_radius=10.0)
        seat = generate_seats(section, [1])[0]
        self.assertAlmostEqual(seat.x, 0.0, places=9)
        self.assertAlmostEqual(seat.y, 0.0, places=9)

    def test_tilt(self):
        # ORIGIN pivot_mode pinned explicitly -- see generation.py's module
        # docstring's Round-2 section: the CENTER default is a SEPARATE
        # contract (test_tilt_pivots_on_center_by_default below), and
        # seat_geometry.js's pivotLocal must match this ORIGIN case too.
        section = self.make_section(
            origin_x=0, origin_y=0, seat_pitch=1.0, row_pitch=1.0, rotation=90.0,
            pivot_mode=Section.PivotMode.ORIGIN,
        )
        by = self.by_identity(generate_seats(section, [1, 1]))
        self.assertAlmostEqual(by[("A", "1")][0], 0.0, places=9)
        self.assertAlmostEqual(by[("A", "1")][1], 0.0, places=9)
        self.assertAlmostEqual(by[("B", "1")][0], -1.0, places=9)
        self.assertAlmostEqual(by[("B", "1")][1], 0.0, places=9)

    def test_tilt_pivots_on_center_by_default(self):
        # Round 2's headline fix: pivot_mode defaults to CENTER (the
        # seats_per_row x rows block's midpoint), not the origin corner. A
        # 2-row x 2-seat block at seat_pitch=row_pitch=1.0 has local center
        # (0.5, 0.5); rotating 180 degrees around that point maps local
        # (0, 0) (row A seat 1) to local (1, 1) (row B seat 2) and vice
        # versa, both offset by the section's origin.
        section = self.make_section(
            origin_x=10.0, origin_y=20.0, seat_pitch=1.0, row_pitch=1.0,
            rows=2, seats_per_row=2, rotation=180.0,
        )
        self.assertEqual(section.pivot_mode, Section.PivotMode.CENTER)
        by = self.by_identity(generate_seats(section, [2, 2]))
        self.assertAlmostEqual(by[("A", "1")][0], 11.0, places=9)
        self.assertAlmostEqual(by[("A", "1")][1], 21.0, places=9)
        self.assertAlmostEqual(by[("B", "2")][0], 10.0, places=9)
        self.assertAlmostEqual(by[("B", "2")][1], 20.0, places=9)
        # The center itself (local (0.5, 0.5), i.e. no real seat there for
        # an even 2x2 grid) is the one point rotation leaves untouched --
        # confirmed directly via pivot_xy.
        self.assertAlmostEqual(pivot_xy(section)[0], 10.5, places=9)
        self.assertAlmostEqual(pivot_xy(section)[1], 20.5, places=9)

    def test_custom_pivot_is_invariant_under_rotation(self):
        section = self.make_section(
            origin_x=0.0, origin_y=0.0, seat_pitch=1.0, row_pitch=1.0,
            pivot_mode=Section.PivotMode.CUSTOM, pivot_x=3.0, pivot_y=4.0, rotation=37.0,
        )
        px, py = pivot_xy(section)
        self.assertAlmostEqual(px, 3.0, places=9)
        self.assertAlmostEqual(py, 4.0, places=9)
        # Same pivot world position regardless of how much rotation is
        # applied -- that's the defining property of a pivot.
        section.rotation = -22.0
        section.save(update_fields=["rotation"])
        px2, py2 = pivot_xy(section)
        self.assertAlmostEqual(px2, px, places=9)
        self.assertAlmostEqual(py2, py, places=9)
