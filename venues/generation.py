"""Seat generator (Phase A/B of docs/SEATING.md's seating-chart epic; live
rework per docs/EDITOR.md).

Turns a Section's layout params (origin/pitch/rotation/offset_mode/
row_x_offset/arc_radius) plus a per-row seat-count spec into concrete Seat
rows: row labels from `Section.row_label_scheme`, seat numbers from
`Section.numbering_scheme`, and x/y from one of two geometries (see
`_seat_xy` below) picked purely by which params are set:

- **Grid/raked** (arc_radius=None): a straight block, optionally staggered
  per-row (`offset_mode`/`row_x_offset`) and/or rotated -- see
  `_grid_or_raked_local`.
- **Fanned** (arc_radius set): rows curve along a circular arc IN PLACE --
  see `_fanned_local`.

*** THE CLIENT/SERVER GEOMETRY CONTRACT ***
static/js/seat_geometry.js re-implements this module's math EXACTLY (same
function names/shapes, docstring-linked) so the dashboard chart editor can
render a section's seats live, with zero server round-trips, and have Save
persist coordinates that are bit-for-bit what staff were just looking at.
This is the ONE place the formulas are specified -- if you change the math
here, change seat_geometry.js's matching function too (and vice versa).
`venues/test_generation.py`'s GeometryTests / SharedFormulaContractTests
assert exact coordinates for representative grid/raked/alternating/arc/
tilt params precisely so a drift between the two implementations shows up
as a Python test failure even though the JS side can't be exercised by
pytest.

Both geometries share the SAME two-step composition: compute a "local"
position with local (0, 0) defined as the section's front-row/row-center
reference point (see each `_..._local` function), then rotate that local
point by `section.rotation` degrees around a PIVOT point (`_rotate`, applied
relative to the pivot -- see `_rotation_pivot_local` below), then translate
by `(section.origin_x, section.origin_y)`.

Round 2 (docs/EDITOR.md "Round 2 refinements") makes the pivot itself
configurable via `Section.pivot_mode`/`pivot_x`/`pivot_y`:
- **CENTER** (default): the midpoint of the `seats_per_row` x `rows` block
  (the same bounding box `static/js/chart_editor.js`'s `localWH()` already
  draws the transform-box handles from) -- rotating swings the whole block
  around its own middle, which reads as far more intuitive than the old
  corner-pivot default.
- **ORIGIN**: local (0, 0) -- the section's front-left corner, i.e. Phase
  A/B's original behavior (local (0, 0) maps to `(origin_x, origin_y)`
  post-rotation, unchanged).
- **CUSTOM**: `(pivot_x, pivot_y)`, set by dragging the pivot marker on
  canvas.

Whatever the pivot, its WORLD position never moves as `rotation` changes --
that's what "pivot" means -- so `pivot_xy()` below (mirrored by
`seat_geometry.js`'s `pivotXY`) is simply `origin + pivot_local`, with no
rotation applied, and the editor draws its pivot marker there.

Per the spec's "separate LOGICAL identity from VISUAL geometry" decision,
row labels/seat numbers and x/y are computed independently and BOTH
persisted -- geometry never feeds back into which seat is which. Unlike the
old Phase B hand-drag editor, x/y is no longer independently hand-editable
after generation: docs/EDITOR.md removes per-seat dragging entirely, so a
seat's x/y is *always* whatever these formulas produce for the section's
current params. Save recomputes those coordinates every time: when the edit
leaves the seat roster identical (a move/rotate/re-pitch/arc/accessible
toggle) it updates the existing rows in place (reposition_seats, which keeps
their pks so attached tickets survive); only a roster change (add/remove
seats) falls back to a destructive regenerate (generate_seats). Per-seat
overrides are limited to existence (removed_seats) and accessibility
(accessible_seats), tracked by (row_label, number) identity on the Section
(see its docstring) since those must survive a regenerate that a raw Seat pk
would not.
"""

import math
import string

from .models import Seat, Section

_BASE_LETTERS_SKIP_IO = [c for c in string.ascii_uppercase if c not in ("I", "O")]
_BASE_LETTERS_ALL = list(string.ascii_uppercase)


class SeatGenerationError(Exception):
    """Raised when generate_seats() can't safely (re)generate a section's
    seats. Message is safe to show directly to dashboard staff."""


class SeatRosterChanged(SeatGenerationError):
    """Raised by reposition_seats() when the section's would-be seat roster
    (the set of (row_label, number) identities) differs from what's already
    persisted -- i.e. this edit adds or removes seats, so it can't be applied
    as an in-place move and has to go through the destructive generate_seats
    path (which enforces the live-ticket guardrail). A subclass of
    SeatGenerationError so a caller that only cares "the layout couldn't be
    applied cleanly" can catch the base; chart_editor_save catches THIS
    specifically to fall back to a full regenerate."""


def generate_row_labels(count, scheme, start=0):
    """`count` row labels in generation order: A, B, C, … through the
    scheme's alphabet, then AA, BB, CC, … (doubled letters, not full base-26
    combinations) once that's exhausted, then AAA, BBB, … and so on.
    `scheme` skips I/O by default (Section.RowLabelScheme.SKIP_IO) to match
    the common house convention of not using letters that are easily
    confused with 1/0.

    `start` (Section.row_label_start) offsets into that same sequence, so a
    section whose first physical row continues the house's letter sequence
    (a Parterre starting at N behind an A-M Orchestra) labels correctly:
    generate_row_labels(3, SKIP_IO, start=12) -> ["N", "P", "Q"]. Mirrored
    by seat_geometry.js's generateRowLabels.
    """
    letters = (
        _BASE_LETTERS_ALL if scheme == Section.RowLabelScheme.ALL_LETTERS else _BASE_LETTERS_SKIP_IO
    )
    n = len(letters)
    labels = []
    for i in range(start, start + count):
        group, index = divmod(i, n)
        labels.append(letters[index] * (group + 1))
    return labels


def generate_seat_numbers(seat_count, scheme, row_index, base=0):
    """Seat numbers (ints), left-to-right, for one row of `seat_count`
    seats. `row_index` is 0-based and only affects the 'hundreds' scheme
    (row A -> 100s, row B -> 200s, …). `base` (Section.seat_number_base) is
    added to every number, composing with the scheme -- e.g. odd_desc_left
    with base=100 numbers a center block ...119, 117 ... 103, 101, the
    common "side blocks odd/even, center block same but in the 100s"
    convention. Numbers are purely a *label* sequence -- they never drive
    the seat's x/y position, which comes from left-to-right physical order
    regardless of numbering scheme (see generate_seats). Mirrored by
    seat_geometry.js's generateSeatNumbers."""
    if scheme == Section.NumberingScheme.ODD_DESC_LEFT:
        # Highest odd number on the house-left end, descending toward the
        # aisle -- e.g. a 4-seat row is 7, 5, 3, 1.
        return [base + 2 * (seat_count - i) - 1 for i in range(seat_count)]
    if scheme == Section.NumberingScheme.EVEN_ASC_RIGHT:
        # Ascending even numbers left to right -- e.g. 2, 4, 6, 8.
        return [base + 2 * (i + 1) for i in range(seat_count)]
    if scheme == Section.NumberingScheme.HUNDREDS:
        row_base = (row_index + 1) * 100
        return [base + row_base + i + 1 for i in range(seat_count)]
    if scheme == Section.NumberingScheme.HUNDREDS_FLAT:
        # Continental center-block style: every row restarts at 101 (the
        # 100-band marks "center section", not the row) -- e.g. row A
        # 101-109, row B 101-110. Row identity comes from the row label.
        # (Equivalent to SEQUENTIAL with base=100; kept as its own scheme
        # since it predates seat_number_base.)
        return [base + 100 + i + 1 for i in range(seat_count)]
    # SEQUENTIAL, and the fallback for any unrecognized value.
    return [base + i + 1 for i in range(seat_count)]


def compute_row_counts(rows, seats_per_row, offset_mode, alt_row_seat_delta):
    """The seat-count shape for a section, front-to-back: `rows` counts of
    `seats_per_row`, except in ALTERNATING `offset_mode` every OTHER row
    (0-based row_index 1, 3, 5…) gets `seats_per_row + alt_row_seat_delta`
    instead -- `alt_row_seat_delta` can be negative to drop seats on those
    rows (a brick/stadium stagger). Every row is floored at 1 seat.
    Mirrored exactly by seat_geometry.js's `computeRowCounts` -- see this
    module's docstring for the client/server contract."""
    counts = []
    for row_index in range(rows):
        n = seats_per_row
        if offset_mode == Section.OffsetMode.ALTERNATING and row_index % 2 == 1:
            n += alt_row_seat_delta
        counts.append(max(1, n))
    return counts


def _rotate(x, y, degrees):
    """Rotate local point (x, y) around local (0, 0) by `degrees` clockwise
    (standard screen-space rotation: y grows downward). Shared by both
    geometries so `rotation` pivots identically -- see module docstring."""
    if not degrees:
        return x, y
    theta = math.radians(degrees)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return x * cos_t - y * sin_t, x * sin_t + y * cos_t


def _row_x_offset(section, row_index):
    """The per-row local-x stagger before rotation, per `offset_mode` -- see
    Section.row_x_offset's help text. REPEATED grows with row_index (Phase
    B's original raked mechanism); ALTERNATING applies the same constant
    amount to every other row only (row_index 1, 3, 5…), for a brick/
    stadium stagger. Mirrored by seat_geometry.js's `rowXOffset`."""
    if section.offset_mode == Section.OffsetMode.ALTERNATING:
        return section.row_x_offset if row_index % 2 == 1 else 0.0
    return row_index * section.row_x_offset


def _grid_or_raked_local(section, row_index, seat_index):
    """Local (pre-rotation, pre-origin) grid/raked position for the seat at
    (row_index, seat_index) -- 0-based, left-to-right/front-to-back.

    Seats run along local x spaced by `seat_pitch`, plus `_row_x_offset`
    (see its docstring for REPEATED vs ALTERNATING); rows step back along
    local y spaced by `row_pitch`. Local (0, 0) is seat (0, 0) -- the
    section's front-left seat before any offset -- which is also the
    rotation pivot (see module docstring): `row_x_offset=0` (either mode)
    reproduces a plain grid exactly.
    """
    local_x = seat_index * section.seat_pitch + _row_x_offset(section, row_index)
    local_y = row_index * section.row_pitch
    return local_x, local_y


def _fanned_local(section, row_index, seat_index, row_seat_count):
    """Local (pre-rotation, pre-origin) fanned/curved position for the seat
    at (row_index, seat_index) in a row of `row_seat_count` seats -- e.g. a
    center orchestra block that curves around a focal point near the stage.

    Row `row_index` sits on a circular arc of `radius = arc_radius +
    row_index * row_pitch`, centered on local point (0, -arc_radius) -- i.e.
    a focal point `arc_radius` BEHIND local (0, 0), so the front row
    (row_index=0) always passes through local (0, 0) *regardless of
    arc_radius's magnitude*: `local_y = radius * cos(theta) - arc_radius`,
    and at row_index=0/theta=0 that's `arc_radius - arc_radius = 0`. This is
    the "curve in place" fix -- arc_radius controls curvature only, never
    where the section sits (the old formula used local_y = radius*cos(theta)
    with no `- arc_radius` correction, which translated the whole section
    away from its origin by `arc_radius`). Seats within a row are spaced by
    arc length `seat_pitch` (angle_step = seat_pitch / radius radians) and
    centered on the row's midpoint, so the row is symmetric around its
    center seat -- and, because cos(theta) <= 1, seats away from center sit
    at a smaller local_y than the center seat, bowing the row's ends toward
    the origin/stage (a real theater's concave curvature).

    Round-4 correction (docs/EDITOR.md): offset now COMPOSES with arc --
    `_row_x_offset(section, row_index)` (the same per-row local-x stagger
    `_grid_or_raked_local` applies) is added on top of the curved
    `radius * sin(theta)` term, so a fanned section can also carry a
    repeated/alternating row stagger. This can't disturb the "curve in
    place"/front-center invariants above: `_row_x_offset` is 0 at
    `row_index=0` in BOTH offset modes (REPEATED is `row_index *
    row_x_offset`; ALTERNATING only touches odd row_index), so the front
    row's local (0, 0) is untouched regardless of row_x_offset -- see
    `front_center_local`'s "arc_radius truthy -> (0, 0)" branch, still
    correct with offset applied.
    """
    radius = section.arc_radius + row_index * section.row_pitch
    angle_step = section.seat_pitch / radius if radius else 0.0
    center_offset = (row_seat_count - 1) / 2
    theta = (seat_index - center_offset) * angle_step
    local_x = radius * math.sin(theta) + _row_x_offset(section, row_index)
    local_y = radius * math.cos(theta) - section.arc_radius
    return local_x, local_y


def _rotation_pivot_local(section):
    """The local (pre-rotation) point `rotation` pivots around, per
    `section.pivot_mode` -- see the module docstring's Round 2 section.
    Mirrored exactly by seat_geometry.js's `pivotLocal`."""
    if section.pivot_mode == Section.PivotMode.ORIGIN:
        return 0.0, 0.0
    if section.pivot_mode == Section.PivotMode.CUSTOM:
        return section.pivot_x, section.pivot_y
    # CENTER (default): midpoint of the seats_per_row x rows block, using
    # the section's SHAPE params -- not the actual (possibly ragged/
    # alternating) row_counts passed to generate_seats, same simplification
    # the editor's transform-box bounding box already makes.
    width = max(0, section.seats_per_row - 1) * section.seat_pitch
    height = max(0, section.rows - 1) * section.row_pitch
    return width / 2.0, height / 2.0


def pivot_xy(section):
    """World (x, y) of `section`'s rotation pivot -- `origin + pivot_local`,
    with NO rotation applied (a pivot's world position is, by definition,
    invariant under the rotation it pivots -- see the module docstring).
    Exposed so the editor can draw a pivot marker; mirrored by
    seat_geometry.js's `pivotXY`."""
    pivot_x, pivot_y = _rotation_pivot_local(section)
    return section.origin_x + pivot_x, section.origin_y + pivot_y


def front_center_local(section, row_seat_count, arc_radius):
    """Local (pre-rotation) position of the section's FRONT-CENTER
    reference point -- row_index=0, the row's midpoint -- evaluated for a
    HYPOTHETICAL `arc_radius` rather than `section.arc_radius`, so callers
    can compare a section's shape "as grid/raked" against "as fanned"
    without mutating it (see `rebalance_origin_for_arc_change` below, the
    Round-3 "arc still offsets the section" fix, docs/EDITOR.md #6).

    Grid/raked (`arc_radius` falsy): `_grid_or_raked_local`'s local (0, 0)
    is the front-LEFT seat, not front-center (that's Phase A's original,
    still-pinned convention -- see GeometryTests.test_grid_is_a_plain_
    rectangle), so front-center is `_grid_or_raked_local`'s row-0 point at
    `seat_index = (row_seat_count - 1) / 2` (the row's midpoint, matching
    `_fanned_local`'s `center_offset` -- interpolated, not rounded, so it's
    exact even for an even seat count with no literal center seat).

    Fanned (`arc_radius` truthy): `_fanned_local`'s local (0, 0) IS the
    front-center point by construction (theta=0 at `seat_index =
    center_offset` -- see its docstring), for ANY `arc_radius` -- that's
    the existing, already-correct "curve in place" invariant
    (GeometryTests.test_arc_radius_does_not_translate_the_section).
    """
    if arc_radius:
        return 0.0, 0.0
    center_offset = (row_seat_count - 1) / 2.0
    return center_offset * section.seat_pitch, 0.0


def front_center_xy(section, row_seat_count, arc_radius):
    """World (x, y) of `front_center_local`, run through the same
    pivot-rotate-translate pipeline `_seat_xy` uses -- mirrored by
    seat_geometry.js's `frontCenterXY`."""
    local_x, local_y = front_center_local(section, row_seat_count, arc_radius)
    pivot_x, pivot_y = _rotation_pivot_local(section)
    rel_x, rel_y = _rotate(local_x - pivot_x, local_y - pivot_y, section.rotation)
    return section.origin_x + pivot_x + rel_x, section.origin_y + pivot_y + rel_y


def _shape_center_local(section, arc_radius, row_seat_count):
    """Local (pre-rotation) bounding-box center of the section's uniform
    rows x row_seat_count seat block, evaluated for a HYPOTHETICAL arc_radius
    (temporarily set + restored, so `section` is NOT mutated). Uses the uniform
    shape params -- the same simplification the pivot / transform box make.
    Mirrored by seat_geometry.js's `shapeCenterLocal`."""
    saved = section.arc_radius
    section.arc_radius = arc_radius
    try:
        xs, ys = [], []
        for r in range(section.rows):
            for c in range(row_seat_count):
                if arc_radius:
                    lx, ly = _fanned_local(section, r, c, row_seat_count)
                else:
                    lx, ly = _grid_or_raked_local(section, r, c)
                xs.append(lx)
                ys.append(ly)
    finally:
        section.arc_radius = saved
    if not xs:
        return 0.0, 0.0
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def shape_center_xy(section, arc_radius, row_seat_count):
    """World (x, y) of `_shape_center_local`, through the same
    pivot-rotate-translate pipeline `_seat_xy` uses. Mirrored by
    seat_geometry.js's `shapeCenterXY`."""
    cx, cy = _shape_center_local(section, arc_radius, row_seat_count)
    pivot_x, pivot_y = _rotation_pivot_local(section)
    rel_x, rel_y = _rotate(cx - pivot_x, cy - pivot_y, section.rotation)
    return section.origin_x + pivot_x + rel_x, section.origin_y + pivot_y + rel_y


def rebalance_origin_for_arc_change(section, new_arc_radius, row_seat_count):
    """The (origin_x, origin_y) that keep `section`'s BOUNDING-BOX CENTER
    exactly where it currently is when `section.arc_radius` is about to change
    to `new_arc_radius`, so the section curves IN PLACE at ANY radius.

    Pinning only the front-CENTER point (the earlier approach) kept the front
    row fixed but let a tightening curve slide the block's center -- measured
    up to ~40% of the block height at small radii -- which is the "arc still
    offsets the section" report. Pinning the bbox center makes that shift zero
    by construction for every radius, and still absorbs the grid-front-LEFT vs
    fanned-front-CENTER local-(0,0) mismatch on enable/disable. Callers
    (chart_editor.js's arc handlers, mirrored by seat_geometry.js's
    `rebalanceOriginForArcChange`) apply this every time arc_radius changes.
    Does not mutate `section`.
    """
    before_x, before_y = shape_center_xy(section, section.arc_radius, row_seat_count)
    after_x, after_y = shape_center_xy(section, new_arc_radius, row_seat_count)
    return section.origin_x + (before_x - after_x), section.origin_y + (before_y - after_y)


def _seat_xy(section, row_index, seat_index, row_seat_count):
    """Dispatch to the right local geometry for `section` (see module
    docstring), then rotate around the section's configured pivot
    (`_rotation_pivot_local`) and translate by the section's origin -- the
    shared final step both geometries go through. `row_seat_count` (the
    row's total seat count) is only used by the fanned case, to center the
    row's arc on its midpoint."""
    if section.arc_radius:
        local_x, local_y = _fanned_local(section, row_index, seat_index, row_seat_count)
    else:
        local_x, local_y = _grid_or_raked_local(section, row_index, seat_index)
    pivot_x, pivot_y = _rotation_pivot_local(section)
    rel_x, rel_y = _rotate(local_x - pivot_x, local_y - pivot_y, section.rotation)
    return section.origin_x + pivot_x + rel_x, section.origin_y + pivot_y + rel_y


def generate_seats(
    section, row_counts, *, accessible=None, removed_ids=None, accessible_ids=None, replace=False
):
    """(Re)generate every Seat in `section` from `row_counts` -- a list of
    per-row seat counts, front-to-back (e.g. [10, 10, 8] for a 3-row section
    with a ragged back row; a uniform NxM grid is just [M] * N, e.g. from
    `compute_row_counts`). Row labels come from `section.row_label_scheme`,
    seat numbers from `section.numbering_scheme`; x/y come from the
    section's grid/raked/fanned geometry (see `_seat_xy` and the module
    docstring).

    `accessible`, if given, is `{row_index: {seat_position, …}}` -- which
    seats to flag `is_accessible=True`, where `row_index` is 0-based and
    `seat_position` is the 1-based LEFT-TO-RIGHT seat position in that row
    (NOT the generated seat number -- numbering schemes like odd_desc_left
    don't run 1..n, so "seat 1" and "position 1" are different things).

    `removed_ids`/`accessible_ids`, if given, are sets of `(row_label,
    number)` string-tuples -- the editor's popover-tracked, regenerate-
    survivable per-seat overrides (see Section.removed_seats/
    accessible_seats). A seat whose (row_label, str(number)) is in
    `removed_ids` is skipped entirely (never created -- e.g. an aisle gap);
    `accessible_ids` is OR'd with the position-based `accessible` dict.

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

    new_seats = _build_seats(section, row_counts, accessible, removed_ids, accessible_ids)
    Seat.objects.bulk_create(new_seats)
    return list(section.seats.order_by("row_label", "number"))


def _build_seats(section, row_counts, accessible, removed_ids, accessible_ids):
    """Build (but DON'T persist) the Seat instances `section`'s current params
    imply for `row_counts` -- the shared inner loop of generate_seats (which
    bulk_creates them) and reposition_seats (which matches them by identity
    onto already-persisted rows). See generate_seats for the meaning of
    `accessible`/`removed_ids`/`accessible_ids`. Returns Seats in
    row-then-generated-number order; each carries its row_label/number
    identity and freshly-computed x/y/is_accessible."""
    accessible = accessible or {}
    removed_ids = removed_ids or set()
    accessible_ids = accessible_ids or set()
    labels = generate_row_labels(len(row_counts), section.row_label_scheme, section.row_label_start)
    seats = []
    for row_index, (row_label, seat_count) in enumerate(zip(labels, row_counts)):
        numbers = generate_seat_numbers(
            seat_count, section.numbering_scheme, row_index, section.seat_number_base
        )
        row_accessible = accessible.get(row_index, set())
        for seat_index, number in enumerate(numbers):
            number_str = str(number)
            identity = (row_label, number_str)
            if identity in removed_ids:
                continue
            x, y = _seat_xy(section, row_index, seat_index, seat_count)
            seats.append(
                Seat(
                    organization=section.organization,
                    section=section,
                    row_label=row_label,
                    number=number_str,
                    x=x,
                    y=y,
                    is_accessible=(seat_index + 1) in row_accessible or identity in accessible_ids,
                )
            )
    return seats


def reposition_seats(section, row_counts, *, removed_ids=None, accessible_ids=None):
    """Apply `section`'s current layout params to its EXISTING seats in place
    -- recompute every seat's x/y (and is_accessible) from the new params and
    bulk_update them, WITHOUT deleting/recreating any Seat row. Because the
    Seat pks are preserved, every Ticket/Hold/etc. FK pointing at those seats
    stays attached -- so a pure move/rotate/re-pitch/arc edit is safe even
    when the section has live tickets, which is exactly the case
    generate_seats has to refuse (a delete would SET_NULL those tickets'
    seats, orphaning them).

    This ONLY works when the edit leaves the seat roster identical. If the new
    params would add or remove any (row_label, number) identity -- a
    rows/seats_per_row/offset/numbering/label change, or a different
    removed_seats set -- there's no faithful 1:1 mapping onto the existing
    rows, so this raises SeatRosterChanged and the caller must fall back to
    the destructive generate_seats path (which is where the live-ticket
    guardrail lives). Returns the updated Seats in row_label/number order."""
    removed_ids = removed_ids or set()
    accessible_ids = accessible_ids or set()
    prospective = _build_seats(section, row_counts, None, removed_ids, accessible_ids)
    prospective_by_identity = {(s.row_label, s.number): s for s in prospective}

    existing = list(section.seats.all())
    existing_by_identity = {(s.row_label, s.number): s for s in existing}
    if set(prospective_by_identity) != set(existing_by_identity):
        raise SeatRosterChanged(
            f"Section {section.name!r}'s seat roster changed -- cannot reposition in place."
        )

    for identity, target in prospective_by_identity.items():
        seat = existing_by_identity[identity]
        seat.x = target.x
        seat.y = target.y
        seat.is_accessible = target.is_accessible
    if existing:
        Seat.objects.bulk_update(existing, ["x", "y", "is_accessible"])
    return list(section.seats.order_by("row_label", "number"))
