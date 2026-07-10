# Chart layout editor — rework spec (live, param-driven, in-canvas)

Supersedes the Phase-B hand-drag editor. Driven by real user feedback after
testing. The editor is a live design tool for building a section's seat block,
not a per-seat drag canvas.

## Philosophy
- **Live**: the seat map re-renders immediately as controls or on-canvas handles
  change. **No "Regenerate" button anywhere** — changes apply live.
- **Param-driven**: a section's seats are computed from its parameters (position,
  rows, seats/row, pitch, tilt, offset, arc). You shape the SECTION; you do not
  hand-place individual seats.
- **No individual seat dragging** — remove it entirely.
- **No separate "Seats list" page** — per-seat actions (ADA toggle, delete) happen
  in a popover on the seat in the canvas.

## Live geometry (client + server must match)
Port the seat-position math from `venues/generation.py` into a JS module so the
canvas updates live with zero server round-trips. `venues/generation.py` stays
authoritative and runs on **Save** (recompute + persist Seat x/y, row_label,
number, is_accessible, and honor deletions). Client and server use the SAME
formulas — factor the geometry so they can't drift (document the formulas in one
place; mirror them). A change to a control/handle updates the JS geometry → seats
move live; Save persists.

## Section parameters (extend the model; ship migrations)
- position/origin (x,y), rows, seats-per-row, seat_pitch, row_pitch.
- **tilt/rotation** (degrees): rotates the whole section's seats around a DEFINED
  pivot. Pick the pivot = the section's own origin corner (document it) and render
  a small pivot marker on canvas so it's unambiguous. Rotation MUST visibly work.
- **offset_mode**: `repeated` | `alternating`.
  - `repeated`: every row shifts by a constant `row_x_offset` (existing raked
    behavior).
  - `alternating`: offset only every OTHER row by the amount (brick/stadium
    stagger), with an option to **add or drop N seats** on the alternating rows
    (e.g. alt rows have +1 / -1 / +0 seats) — a small int like `alt_row_seat_delta`.
  Add `offset_mode` + `alt_row_seat_delta` (and keep `row_x_offset`) to Section.
- **arc_radius**: curves the rows along an arc **IN PLACE**. BUG TO FIX: today it
  translates the whole group away from origin by the radius. Correct it so the
  section stays at its position and arc only bends the rows (seats fan along the
  arc; radius controls curvature, not translation).

## Controls — semantic, not bare inputs
Each control matches its function:
- **tilt / rotation**: slider with a **centered zero marker** (e.g. -45deg..+45deg).
- **offset amount**: centered slider.
- **arc**: slider (straight at one end → tighter curve at the other).
- **offset mode**: a two-option toggle; alternating reveals the add/drop-seats control.
- rows / seats-per-row / pitch: steppers (numeric is fine for counts).
- numbering_scheme, row_label_scheme: selects.
Live-bind every control to the geometry.

## Canvas — navigation, selection, transform
- **Navigation**: wheel-zoom centered on cursor, pan (drag empty background),
  Fit button + a correct initial fit (tight bbox + modest padding, correct for
  mixed grid/raked/fanned). Keep all pointer math **per-axis correct** — FIX the
  vertical-only 0.5x drag/handle bug (the client→viewBox scale must be computed
  correctly for BOTH axes; today Y is half — almost certainly a viewBox-vs-rendered
  aspect / preserveAspectRatio mismatch; compute the true content box and use the
  right scaleX/scaleY, or enforce a matching aspect).
- **Selection**: click a section (or marquee/shift-click a group) to select it.
- **In-canvas transform box** on the selection: a bounding box with handles —
  **corner handles resize** (scale the block → maps to span/pitch), a **rotate
  handle** (around the documented pivot), and **skew/offset handles** (drive the
  offset param). Dragging a handle updates the section params live (and moves the
  actual seats live). This replaces per-seat dragging.

## Per-seat popover (replaces the Seats list)
Clicking an individual seat opens a small popover/tooltip anchored to it with:
- **ADA / accessible** toggle
- **Delete** (removes that seat; deletion persists across live param changes —
  track removed (row,number) identities and re-apply on regenerate).
Remove the standalone seats-list view/route/template.

## Save model
Save writes the section params AND regenerates+persists seats server-side via
`venues/generation.py` (same formulas as the live JS), then applies per-seat
overrides: the removed-seat set (deletions) and ADA flags. Manager-gated,
org-scoped (never touch another org's chart) — unchanged. Refuse/warn if a
regenerate would drop a seat that has a live (non-void) ticket (keep the Phase-A
guardrail).

## Keep / out of scope
- Zone editor (`zone_editor.js`) keeps its marquee pricing flow; give it the SAME
  navigation (zoom/pan/fit) + the vertical-drag fix via the shared viewport module,
  but its zone logic is unchanged.
- No third-party JS — vendored Alpine + inline SVG only. Manager role gates and
  org-scoping unchanged. This is UI + a small Section model change; do not touch
  payments/booking/auth.

## Verification bar
Full pytest green (412 prior + tests for the new offset mode / save-with-overrides
/ arc-in-place geometry — test the geometry math server-side). Then DRIVE it
(Playwright): build a section, watch it update live as tilt/offset/arc sliders
move (no regenerate), switch offset to alternating (+/- seats), rotate via the
handle (visible, around the shown pivot), resize via corner handles, confirm arc
curves in place (section doesn't jump), open a seat popover and toggle ADA / delete,
verify vertical drag tracks the cursor 1:1. Screenshot the live editing. Fix
anything that isn't actually live/usable.

## Round 2 refinements (post-review feedback)
1. **Slider + numeric entry**: every slider (tilt, offset, arc, pitch, ...) pairs
   with a small number input showing/accepting the exact value; the two stay in
   sync (drag slider updates number, type number updates slider + map).
2. **Rotation pivot = section CENTER by default** (not the origin corner), and
   **configurable** — let the user move the pivot (draggable pivot marker and/or a
   center/origin/custom选 selector). Persist if configurable (Section field +
   migration); a plain center default needs no migration.
3. **Responsive handle size**: the on-canvas control dots are too big for mouse.
   Small for fine pointers (desktop), larger for coarse/touch — `@media (pointer:
   coarse)` or equivalent.
4. **Background scale grid**: a light reference grid (with a sense of scale) drawn
   behind the seats in the canvas.
5. **Handle tooltips**: every on-canvas handle has a native mouseover title
   (SVG `<title>`) naming its function (Rotate, Resize, Offset, Pivot, ...).
6. **Seat size must NOT change with spacing** (bug): seat radius is a constant;
   changing seat_pitch/row_pitch changes the GAPS between seats, not the seat
   size. Decouple the drawn seat radius from pitch in both the JS and any export.
7. **Add sections without leaving the editor**: "New section" adds a section
   inline (in-editor form/modal); it appears live on the canvas. No navigation to
   a separate page.

   **Follow-up feedback on #7**: the inline form must stay human-friendly, not a
   dump of internal params. No raw `ordering` number input anywhere (create OR
   edit) — a new section auto-appends to the end of the chart's section list;
   reordering afterward is up/down arrows in the sidebar (swap-with-neighbor),
   not a manual sort integer. No bare `origin_x`/`origin_y` fields either — a new
   section gets an automatic, non-overlapping default position (staggered off
   existing sections, same idea as before) and is placed precisely by dragging on
   canvas. The create form is just Name (required) + Tier (optional label); rows/
   seats-per-row/pitch keep sensible model defaults so the section is immediately
   visible and usable on the canvas.
