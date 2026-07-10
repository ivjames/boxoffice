/*
 * Seat geometry -- the CLIENT half of the client/server contract documented
 * in venues/generation.py's module docstring. Every function here mirrors a
 * same-named (camelCase vs snake_case) function there EXACTLY: same inputs,
 * same math, same order of operations. If you change one side, change the
 * other -- venues/test_generation.py's SharedFormulaContractTests hard-codes
 * the expected numbers both sides must produce for representative params
 * (grid / repeated-offset / alternating-offset / arc / tilt), so a drift
 * shows up there even though this file can't be exercised by pytest.
 *
 * This module is pure math -- no DOM, no Alpine, no fetch -- so
 * chart_editor.js can call it on every slider/handle input to recompute the
 * live seat list with zero server round-trips (docs/EDITOR.md's "Live, no
 * regenerate" requirement). `venues.generation.generate_seats` runs the
 * SAME formulas server-side on Save and is what actually persists Seat
 * rows -- this module never talks to the server.
 *
 * `section` here is a plain object with (at least): origin_x, origin_y,
 * rotation, seat_pitch, row_pitch, row_x_offset, offset_mode ("repeated" |
 * "alternating"), alt_row_seat_delta, arc_radius (number or null/0 for
 * grid/raked), rows, seats_per_row, numbering_scheme, row_label_scheme,
 * pivot_mode ("center" | "origin" | "custom" -- Round 2, see pivotLocal),
 * pivot_x, pivot_y (CUSTOM pivot_mode only).
 */

const OFFSET_MODE_ALTERNATING = "alternating";

const NUMBERING_SEQUENTIAL = "sequential";
const NUMBERING_ODD_DESC_LEFT = "odd_desc_left";
const NUMBERING_EVEN_ASC_RIGHT = "even_asc_right";
const NUMBERING_HUNDREDS = "hundreds";

const ROW_LABEL_ALL_LETTERS = "all_letters";

const LETTERS_ALL = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");
const LETTERS_SKIP_IO = LETTERS_ALL.filter((c) => c !== "I" && c !== "O");

// -- row labels / seat numbers (mirrors generate_row_labels / generate_seat_numbers) --

function generateRowLabels(count, scheme) {
    const letters = scheme === ROW_LABEL_ALL_LETTERS ? LETTERS_ALL : LETTERS_SKIP_IO;
    const n = letters.length;
    const labels = [];
    for (let i = 0; i < count; i++) {
        const group = Math.floor(i / n);
        const index = i % n;
        labels.push(letters[index].repeat(group + 1));
    }
    return labels;
}

function generateSeatNumbers(seatCount, scheme, rowIndex) {
    if (scheme === NUMBERING_ODD_DESC_LEFT) {
        return Array.from({ length: seatCount }, (_, i) => 2 * (seatCount - i) - 1);
    }
    if (scheme === NUMBERING_EVEN_ASC_RIGHT) {
        return Array.from({ length: seatCount }, (_, i) => 2 * (i + 1));
    }
    if (scheme === NUMBERING_HUNDREDS) {
        const base = (rowIndex + 1) * 100;
        return Array.from({ length: seatCount }, (_, i) => base + i + 1);
    }
    return Array.from({ length: seatCount }, (_, i) => i + 1);
}

// -- row shape (mirrors compute_row_counts) --

function computeRowCounts(rows, seatsPerRow, offsetMode, altRowSeatDelta) {
    const counts = [];
    for (let rowIndex = 0; rowIndex < rows; rowIndex++) {
        let n = seatsPerRow;
        if (offsetMode === OFFSET_MODE_ALTERNATING && rowIndex % 2 === 1) {
            n += altRowSeatDelta;
        }
        counts.push(Math.max(1, n));
    }
    return counts;
}

// -- geometry (mirrors _rotate / _row_x_offset / _grid_or_raked_local / _fanned_local / _seat_xy) --

function rotate(x, y, degrees) {
    if (!degrees) return [x, y];
    const theta = (degrees * Math.PI) / 180;
    const cosT = Math.cos(theta);
    const sinT = Math.sin(theta);
    return [x * cosT - y * sinT, x * sinT + y * cosT];
}

function rowXOffset(section, rowIndex) {
    if (section.offset_mode === OFFSET_MODE_ALTERNATING) {
        return rowIndex % 2 === 1 ? section.row_x_offset : 0.0;
    }
    return rowIndex * section.row_x_offset;
}

function gridOrRakedLocal(section, rowIndex, seatIndex) {
    const localX = seatIndex * section.seat_pitch + rowXOffset(section, rowIndex);
    const localY = rowIndex * section.row_pitch;
    return [localX, localY];
}

function fannedLocal(section, rowIndex, seatIndex, rowSeatCount) {
    const radius = section.arc_radius + rowIndex * section.row_pitch;
    const angleStep = radius ? section.seat_pitch / radius : 0.0;
    const centerOffset = (rowSeatCount - 1) / 2;
    const theta = (seatIndex - centerOffset) * angleStep;
    const localX = radius * Math.sin(theta);
    const localY = radius * Math.cos(theta) - section.arc_radius;
    return [localX, localY];
}

function seatXY(section, rowIndex, seatIndex, rowSeatCount) {
    let local;
    if (section.arc_radius) {
        local = fannedLocal(section, rowIndex, seatIndex, rowSeatCount);
    } else {
        local = gridOrRakedLocal(section, rowIndex, seatIndex);
    }
    const [px, py] = pivotLocal(section);
    const [relX, relY] = rotate(local[0] - px, local[1] - py, section.rotation);
    return [section.origin_x + px + relX, section.origin_y + py + relY];
}

/*
 * The local (pre-rotation) point `rotation` pivots around, per
 * `section.pivot_mode` -- mirrors generation.py's `_rotation_pivot_local`
 * EXACTLY (see this file's header and that function's docstring for the
 * CENTER/ORIGIN/CUSTOM contract).
 */
function pivotLocal(section) {
    if (section.pivot_mode === "origin") return [0, 0];
    if (section.pivot_mode === "custom") return [section.pivot_x || 0, section.pivot_y || 0];
    // CENTER (default).
    const w = Math.max(0, section.seats_per_row - 1) * section.seat_pitch;
    const h = Math.max(0, section.rows - 1) * section.row_pitch;
    return [w / 2, h / 2];
}

/*
 * World (x, y) of the section's rotation pivot -- origin + pivotLocal, with
 * NO rotation applied (a pivot's world position is invariant under the
 * rotation it pivots around -- see generation.py's module docstring).
 * Mirrors generation.py's `pivot_xy`.
 */
function pivotXY(section) {
    const [px, py] = pivotLocal(section);
    return [section.origin_x + px, section.origin_y + py];
}

/*
 * Full live seat list for `section` -- mirrors venues.generation.
 * generate_seats's per-seat loop (labels/numbers/xy/is_accessible), minus
 * the DB round-trip, PLUS the removedIds/accessibleIds identity overrides
 * (Set of "row|number" strings -- see chart_editor.js's seatKey helper).
 * Returns [{row, number, x, y, accessible, removed}], WITHOUT removed seats
 * filtered out) so the caller can decide whether to render/skip a removed
 * seat (an editor might want to show a ghost seat for a deleted one; the
 * server's generate_seats always fully drops it).
 */
function computeSectionSeats(section, { removedIds, accessibleIds } = {}) {
    removedIds = removedIds || new Set();
    accessibleIds = accessibleIds || new Set();
    const rowCounts = computeRowCounts(
        section.rows, section.seats_per_row, section.offset_mode, section.alt_row_seat_delta
    );
    const labels = generateRowLabels(rowCounts.length, section.row_label_scheme);
    const seats = [];
    rowCounts.forEach((seatCount, rowIndex) => {
        const rowLabel = labels[rowIndex];
        const numbers = generateSeatNumbers(seatCount, section.numbering_scheme, rowIndex);
        numbers.forEach((number, seatIndex) => {
            const numberStr = String(number);
            const key = rowLabel + "|" + numberStr;
            const [x, y] = seatXY(section, rowIndex, seatIndex, seatCount);
            seats.push({
                row: rowLabel,
                number: numberStr,
                x,
                y,
                accessible: accessibleIds.has(key),
                removed: removedIds.has(key),
            });
        });
    });
    return seats;
}

window.SeatGeometry = {
    generateRowLabels,
    generateSeatNumbers,
    computeRowCounts,
    rotate,
    rowXOffset,
    gridOrRakedLocal,
    fannedLocal,
    seatXY,
    pivotLocal,
    pivotXY,
    computeSectionSeats,
};
