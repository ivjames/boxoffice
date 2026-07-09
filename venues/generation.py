"""Seat generator (Phase A of docs/SEATING.md's seating-chart epic).

Turns a Section's layout params (origin/pitch/rotation) plus a per-row
seat-count spec into concrete Seat rows: row labels from
`Section.row_label_scheme`, seat numbers from `Section.numbering_scheme`,
and x/y from a straight grid anchored at the section's origin.

Per the spec's "separate LOGICAL identity from VISUAL geometry" decision,
row labels/seat numbers and x/y are computed independently and BOTH
persisted -- geometry never feeds back into which seat is which, and the
generated x/y is just a starting point (Phase B's drag editor is where a
bespoke house gets its final hand-placed positions).

Raked (growing per-row x-offset) and fanned/arc-radius layouts are NOT
implemented here -- Phase A always produces a straight grid, optionally
rotated as a whole around the section's origin. That's the "leave the hook"
scope line from the epic brief: `arc_radius` and per-row offsets are
Phase B's job.
"""

import math
import string

from .models import Seat, Section

_BASE_LETTERS_SKIP_IO = [c for c in string.ascii_uppercase if c not in ("I", "O")]
_BASE_LETTERS_ALL = list(string.ascii_uppercase)


class SeatGenerationError(Exception):
    """Raised when generate_seats() can't safely (re)generate a section's
    seats. Message is safe to show directly to dashboard staff."""


def generate_row_labels(count, scheme):
    """`count` row labels in generation order: A, B, C, … through the
    scheme's alphabet, then AA, BB, CC, … (doubled letters, not full base-26
    combinations) once that's exhausted, then AAA, BBB, … and so on.
    `scheme` skips I/O by default (Section.RowLabelScheme.SKIP_IO) to match
    the common house convention of not using letters that are easily
    confused with 1/0.
    """
    letters = (
        _BASE_LETTERS_ALL if scheme == Section.RowLabelScheme.ALL_LETTERS else _BASE_LETTERS_SKIP_IO
    )
    n = len(letters)
    labels = []
    for i in range(count):
        group, index = divmod(i, n)
        labels.append(letters[index] * (group + 1))
    return labels


def generate_seat_numbers(seat_count, scheme, row_index):
    """Seat numbers (ints), left-to-right, for one row of `seat_count`
    seats. `row_index` is 0-based and only affects the 'hundreds' scheme
    (row A -> 100s, row B -> 200s, …). Numbers are purely a *label*
    sequence -- they never drive the seat's x/y position, which comes from
    left-to-right physical order regardless of numbering scheme (see
    generate_seats)."""
    if scheme == Section.NumberingScheme.ODD_DESC_LEFT:
        # Highest odd number on the house-left end, descending toward the
        # aisle -- e.g. a 4-seat row is 7, 5, 3, 1.
        return [2 * (seat_count - i) - 1 for i in range(seat_count)]
    if scheme == Section.NumberingScheme.EVEN_ASC_RIGHT:
        # Ascending even numbers left to right -- e.g. 2, 4, 6, 8.
        return [2 * (i + 1) for i in range(seat_count)]
    if scheme == Section.NumberingScheme.HUNDREDS:
        base = (row_index + 1) * 100
        return [base + i + 1 for i in range(seat_count)]
    # SEQUENTIAL, and the fallback for any unrecognized value.
    return [i + 1 for i in range(seat_count)]


def _seat_xy(section, row_index, seat_index):
    """Straight-grid position for the seat at (row_index, seat_index) --
    0-based, left-to-right/front-to-back -- spaced by the section's
    seat_pitch/row_pitch from its origin, rotated `rotation` degrees around
    that origin. `arc_radius` is intentionally ignored here (Phase B)."""
    local_x = seat_index * section.seat_pitch
    local_y = row_index * section.row_pitch
    if section.rotation:
        theta = math.radians(section.rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        local_x, local_y = (
            local_x * cos_t - local_y * sin_t,
            local_x * sin_t + local_y * cos_t,
        )
    return section.origin_x + local_x, section.origin_y + local_y


def generate_seats(section, row_counts, *, accessible=None, replace=False):
    """(Re)generate every Seat in `section` from `row_counts` -- a list of
    per-row seat counts, front-to-back (e.g. [10, 10, 8] for a 3-row section
    with a ragged back row; a uniform NxM grid is just [M] * N). Row labels
    come from `section.row_label_scheme`, seat numbers from
    `section.numbering_scheme`; x/y are a straight grid from the section's
    origin/pitch/rotation (see `_seat_xy`).

    `accessible`, if given, is `{row_index: {seat_position, …}}` -- which
    seats to flag `is_accessible=True`, where `row_index` is 0-based and
    `seat_position` is the 1-based LEFT-TO-RIGHT seat position in that row
    (NOT the generated seat number -- numbering schemes like odd_desc_left
    don't run 1..n, so "seat 1" and "position 1" are different things).

    Regeneration is destructive by design (this is a bulk generator, not an
    editor -- Phase B's drag/toggle editing is for one-off hand adjustment):
    if the section already has seats, this refuses unless `replace=True`.
    Even with `replace=True` it refuses outright if any of those seats back
    a live (non-void) Ticket, full stop -- deleting a seat under an issued
    ticket is never safe, so that has to be resolved by hand (void/refund
    the ticket, or build a new section) rather than the generator silently
    orphaning it. Returns the freshly generated Seats (row_label/number
    order).
    """
    from orders.models import Ticket  # local import: orders imports venues, not vice versa

    accessible = accessible or {}
    existing = list(section.seats.all())
    if existing:
        live_ticket_seats = (
            Ticket.objects.filter(seat__section=section).exclude(status=Ticket.Status.VOID).count()
        )
        if live_ticket_seats:
            raise SeatGenerationError(
                f"Section {section.name!r} has {live_ticket_seats} seat(s) with a live ticket "
                "issued -- regenerating would orphan them. Void/refund those tickets first, or "
                "build a new section instead."
            )
        if not replace:
            raise SeatGenerationError(
                f"Section {section.name!r} already has {len(existing)} seat(s). Pass "
                "replace=True to delete and regenerate them (safe -- no live tickets exist)."
            )
        Seat.objects.filter(pk__in=[s.pk for s in existing]).delete()

    labels = generate_row_labels(len(row_counts), section.row_label_scheme)
    new_seats = []
    for row_index, (row_label, seat_count) in enumerate(zip(labels, row_counts)):
        numbers = generate_seat_numbers(seat_count, section.numbering_scheme, row_index)
        row_accessible = accessible.get(row_index, set())
        for seat_index, number in enumerate(numbers):
            x, y = _seat_xy(section, row_index, seat_index)
            new_seats.append(
                Seat(
                    organization=section.organization,
                    section=section,
                    row_label=row_label,
                    number=str(number),
                    x=x,
                    y=y,
                    is_accessible=(seat_index + 1) in row_accessible,
                )
            )
    Seat.objects.bulk_create(new_seats)
    return list(section.seats.order_by("row_label", "number"))
