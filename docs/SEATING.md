# Seating charts, visual pricing zones & export — design spec

Epic branch: `claude/seating-charts` (off `main`). Goal: make venue onboarding
self-serve (build any house, including irregular ones), make per-performance
pricing **visual** (drag-select seats into priced zones), and export a
zone map to PDF/PNG. `main` stays stable/deployable throughout.

## Guiding principle: separate LOGICAL identity from VISUAL geometry

The single most important decision (validated against a real raked/diagonal
house — 3 tiers × L/C/R, odd-left / even-right / hundreds-center numbering,
skipped I/O rows, ragged row lengths, wheelchair squares):

- **Logical identity** — what sales/pricing/tickets need: `Section → row(label)
  → Seat(number, accessible)`. Ragged rows, skipped letters, and odd/even/100s
  numbering are *just labels*; the system only needs uniqueness. This is fully
  spreadsheet/JSON-shaped and imports cleanly for ~95% of venues.
- **Visual geometry** — `Seat.x/y` (already in the model, already rendered by the
  Phase-8 seat map). Derived, persisted, and the ONLY thing weird layouts affect.
  **Geometry never touches the money path**, so the most bespoke diagonal house
  cannot affect booking correctness.

## Data model changes (additive)

- `Section` gains layout params: `origin_x/origin_y`, `rotation` (deg),
  `seat_pitch`, `row_pitch`, optional `arc_radius` (fanned center), plus authoring
  metadata: `numbering_scheme` (odd-desc-left | even-asc-right | hundreds |
  sequential), `row_label_scheme` (skip I/O by default), and an optional `tier`
  grouping (Orchestra/Parterre/Balcony). x/y is generated from these, then
  hand-adjustable.
- `Performance.seating_chart` FK — make chart selection explicit (today a
  performance implicitly uses the venue's first chart). Enables multiple charts
  per venue and per-performance choice.
- `PerformanceSeatBlock(performance, seat)` — house kills (sightline holds) that
  remove a seat from sale for one performance without deleting it. Availability
  math treats a blocked seat like an unavailable one.
- `PricingZone(performance, name, amount, color)` + `seats` (M2M via a through if
  per-seat metadata is needed) — the visual per-performance pricing groups.

### Price resolution (extends Phase 7)

For a reserved seat on performance P, in section S:
1. `PricingZone` containing that seat for P  → zone price (most specific).
2. else per-performance section override `PriceTier(performance=P, section=S)`.
3. else section default `PriceTier(performance=None, section=S)`.
4. else `PricingError`.
Zones are always resolved **server-side**; the client never sends a price.

## Authoring — three layers, escalate only as needed

1. **Logical + JSON/CSV import** (covers most houses): section → rows → seats
   with numbering schemes and skipped letters. Import/export so a house is
   defined/backed-up/cloned once. Dashboard "chart builder": create chart → add
   sections → bulk-generate rows×seats grid → toggle individual seats (aisles,
   accessible).
2. **Geometry generator**: compute x/y from section layout params. Plain grid =
   rotation 0; **raked side sections = rotation + growing per-row x_offset**;
   fanned center = arc. Handles the common irregular shapes without hand-placing.
3. **Visual editor** (the last mile): drag seats to final positions for truly
   bespoke houses; dragged x/y wins. This is where the diagonal theater is
   finished. *(Scope/UX decision — see Open questions.)*

## Visual pricing zones (per performance)

On the seat map, staff select seats — rubber-band drag and/or click/shift-click —
and assign the selection to a named, colored `PricingZone` with a price, scoped to
that performance. Section defaults remain the baseline; zones are the granular
override. Reflected live on the storefront seat picker (seat color/price by zone).
*(UX mechanics are an Open question.)*

## Export PDF/PNG of zones per performance

Render the performance's zone map (seats colored by zone, legend, labels) to
PNG/PDF for box-office reference sheets and marketing. **Reuse the already-installed
Chromium + Playwright** (`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`) to render the
map view server-side to an image/PDF — no new heavy dependency. A management
command and a dashboard "Export" button.

## Build phases (on this branch; each = one delegated agent, reviewed)

- **A. Foundation (no-regret):** Section layout params + `Performance.seating_chart`
  FK + `PerformanceSeatBlock` + JSON/CSV import/export + dashboard chart CRUD &
  grid/section generator. Migrations + tests. Storefront/availability honor blocks.
- **B. Geometry + visual editor:** generate x/y from params (grid/raked/fanned);
  drag-to-place editor. *(needs UX steer)*
- **C. Drag-select pricing zones:** zone model + selection UI + price resolution +
  storefront/checkout integration + tests. *(needs UX steer)*
- **D. Export:** Chromium-rendered PNG/PDF of a performance's zone map.

## Decisions (locked)

1. **Drag-select mechanics:** marquee (rubber-band) + shift-click to accumulate.
2. **Zones:** reusable named/colored zone **templates** — define once, apply/clone
   onto any performance. A performance's zone assignment is its own instance;
   editing one performance never mutates another.
3. **Visual editor:** in-house **SVG drag** (lightweight, no canvas library).
4. **Export:** **PNG and PDF both**. Seat labels + price legend are **optional**
   (toggle, default on). Paper size **Letter by default, Legal optional**.
