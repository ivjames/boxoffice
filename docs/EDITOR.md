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

## Round 3 refinements (iPad + desktop testing)
The on-canvas TRANSFORM SYSTEM is the crux — it's currently broken. Priority order:
1. **Transform handles broken/missing**: corner (resize) handles AND the rotation
   handle don't render/aren't usable. Rebuild the transform frame so it reliably
   shows 4 corner resize handles, a rotation handle, offset/skew handle(s), and the
   pivot marker — all clearly visible and grabbable.
2. **Transform box must ROTATE WITH the section**: when tilt/rotation changes, the
   bounding box + handles rotate to match the block's orientation. Today the frame
   doesn't follow the rotation.
3. **Chunky, reliable TOUCH targets** (top touch priority): iPad touch is flaky.
   Handles need large, robust hit areas on touch (bigger than the current
   coarse-pointer bump) with proper pointer capture so drags don't drift/drop —
   generous invisible hit zones around each visible handle.
4. **Handles clear of seats + hover CURSORS**: offset/skew handles must not sit on
   top of seats (occlusion); desktop hover shows the right cursor (nwse/nesw-resize
   on corners, a rotate cursor, move cursor) so the action is obvious.
5. **Seat labels missing**: seats MUST show their number/label inside the circle
   (storefront + old editor did). Legible at fit; degrade gracefully when zoomed out.
6. **Arc STILL offsets the section** (survived 2 rounds): fix arc-in-place for real
   and VERIFY in-browser — enabling/tightening arc must NOT move the section
   (anchor front-center). Prove with before/after screenshot.
7. **Pitch controls with arc**: seat/row pitch editing (+ corner handles where they
   map) must stay available when arc is on (they currently vanish).
8. **Offset amount range**: max of 2 is too small — raise to a useful range.
9. **Alternating add/drop = ±1 only**: alt-row seat delta limited to -1/0/+1.
10. **Offset + arc**: offset doesn't work with arc on — either make it work or
    clearly disable+label it for arc sections (low priority; user says maybe fine).
11. **Snap-to-grid**: optional, OFF by default; snaps dragging/positioning to grid
    increments (pairs with the background grid).
12. **JSON import/export**: round-trip the new pivot fields in chart_io.

## Round 4 refinements (continued testing)
1. **Arc slider must NEVER vanish**: sliding arc to 0 currently hides the slider.
   Keep the arc slider ALWAYS visible; 0 = straight (no curve). Decouple the
   slider's presence from the value / any checkbox.
2. **Arc STILL offsets the origin + transform box**: the seats may now stay put,
   but the transform BOUNDING BOX and the origin/pivot MARKERS are still offset
   from the curved seats when arc is applied. FIX by deriving the transform frame
   AND the origin/pivot markers from the ACTUAL rendered seat positions (real
   bounding box of the current seat coordinates + padding) rather than from raw
   origin/rectangular assumptions — so the box and markers correctly wrap the
   seats for ANY geometry (grid / raked / fanned / rotated). Verify in-browser:
   enable arc, confirm the box wraps the curved seats, the origin/pivot markers
   sit correctly, and the seats do not move.
3. **Control points = function ICONS**: replace the plain handle dots with small
   inline-SVG icons of their function — rotate handle = circular-arrow; corner
   resize = diagonal resize arrows; offset/skew = a shift/slide icon; move-section
   = 4-way move icon; pivot = crosshair/target. Keep the large invisible touch
   hit-zones behind each icon.
4. **Control points must NEVER overlap seats**: offset the corner (and all) handles
   OUTWARD beyond the seat block via frame padding, so no handle ever sits on a
   seat — combine with #2's bbox-derived frame + generous padding.

## Round 4 corrections (mid-round user testing)
Two follow-up corrections to round 3's own offset decisions, from the same round
of testing that produced items 1-4 above:
5. **Offset amount capped at 2**: round 3 #8 raised the offset-amount range to a
   seat_pitch-scaled ~20+ — a misread of the user's actual feedback. The user
   wants it LIMITED to a max of 2 (the slider stays centered/bidirectional:
   -2..+2). Enforce server-side too, not just the slider/number input.
6. **Offset composes with arc**: round 3 #10 disabled the offset controls
   outright for arc-enabled sections (offset was a no-op for fanned rows). The
   user wants offset and arc to work TOGETHER — an arc'd section can also carry
   a per-row offset (repeated or alternating), applied relative to the curve.
   Implement in both `seat_geometry.js` and `venues/generation.py` (keep them in
   lockstep, `SharedFormulaContractTests` covering both), and remove the
   "disabled with a note" UI treatment — the offset controls are always
   available now, arc or no arc.

## Round 6 refinements (touch/Safari + grid feedback)
Three fixes from continued real-device testing:
1. **A different solution for touch/Safari**: rounds 3–5 kept enlarging the tiny
   on-canvas move handle and hardening pointer capture, but precisely grabbing a
   ~0.5-unit dot with a finger stays unreliable on iPad/Safari. New approach:
   **move a section by dragging its BODY** — a pointerdown on any seat starts a
   section-move drag (the same `origin` path the handle uses, snap included); a
   clean **tap** (pointer never passes a ~6px threshold) still opens that seat's
   accessible/delete popover. Rotate/resize/offset already have sidebar sliders,
   so touch users never need the small handles at all. Capture is taken on the
   stable `<svg>` root, not the seat circle — `renderSection()` rebuilds every
   seat `<circle>` each drag frame, so a capture on the circle drops the moment
   the section first moves (and iOS Safari then stops delivering pointermove,
   the exact "can't drag" failure). Desktop handles are unchanged.
2. **Snap-to-grid must conform to the grid**: the move drag snapped
   *incrementally* — `round(origin + per-event-delta)` with the reference reset
   each event — so with snap on, every sub-1-unit per-event delta rounded
   straight back to the current origin and the section never landed on a grid
   line. Fixed to compute the new origin from the drag-start origin + the FULL
   pointer delta, then snap once, so a snapped drag clicks cleanly onto whole-
   unit grid squares (default seat/row pitch is 1.0, so the seats line up with
   the minor grid).
3. **Background grid was basically invisible**: the reference grid drew at
   0.015/0.025 SVG user-unit strokes in a near-white border color — a sub-pixel
   ghost at fit zoom. Restyled to a stronger slate color at a visibly heavier
   (but still sub-seat) user-unit width so it reads clearly at fit and holds up
   zoomed in/out. Kept in user units rather than `non-scaling-stroke`, which
   older iPad WebKit ignores inside a `<pattern>` (falling back to a 1-unit
   stroke that would flood each tile solid).
