/*
 * Live, param-driven chart editor (dashboard/templates/dashboard/
 * chart_editor.html) -- docs/EDITOR.md's rework of the old Phase B
 * hand-drag editor. No per-seat dragging, no "Regenerate" button: every
 * control (slider/stepper/select/on-canvas handle) mutates a section's
 * params in local Alpine state and immediately redraws its seats via
 * static/js/seat_geometry.js -- the SAME formulas venues/generation.py
 * runs server-side on Save (see that module's docstring for the contract).
 *
 * Rendering: seat <circle>s are NOT Alpine x-for (a `<template x-for>`
 * root inside <svg> gets parsed with the SVG namespace and breaks Alpine's
 * clone-based x-for -- see templates/orders/_seat_map.html's comment for
 * the confirmed browser behavior) and the seat COUNT itself changes live
 * (rows/seats-per-row/alt-delta steppers), so unlike the old chart_editor.js
 * (which only ever rebound cx/cy on a fixed, server-rendered set of
 * circles), this file owns seat rendering imperatively: each section gets
 * one <g data-section-group> container, and renderSection() clears/rebuilds
 * its <circle> children from scratch via createElementNS on every param
 * change. The transform-box handles and pivot marker, by contrast, are a
 * FIXED small set of elements, so those stay plain Alpine-bound SVG
 * elements (:cx/:cy computed from small pure methods below).
 *
 * Viewport (zoom/pan/fit, axis-correct pointer math) comes from the shared
 * static/js/editor_viewport.js module, included by both this file and
 * zone_editor.js -- see that file for the vertical-0.5x-drag bug fix.
 *
 * Round 4 (docs/EDITOR.md "Round 4 refinements", continued iPad/desktop
 * testing) is two fixes to the transform system plus two corrections to
 * round 3's own offset decisions:
 *   - The arc slider used to vanish (hidden behind an "Arc / curve"
 *     checkbox, and again whenever the amount hit 0) -- the user hated it.
 *     There is no more checkbox: `arc_enabled` is gone entirely, and
 *     `arc_amount`/`arc_radius` alone drive the geometry (0 = straight, the
 *     same as every other slider at its zero value) via onArcAmountInput(),
 *     the sole entry point now. The slider template markup is always
 *     rendered, unconditionally.
 *   - THE BIG ONE: the transform box's corner handles, the rotate/offset
 *     handles, and the origin/pivot MARKERS used to be derived from a
 *     naive rectangle (localWH() -- `(seats_per_row-1)*seat_pitch` x
 *     `(rows-1)*row_pitch`) or fixed pitch-scaled deltas, neither of which
 *     account for arc's curved local geometry -- so enabling/tightening arc
 *     visibly left the box and markers behind while the (already-pinned,
 *     since round 3 #6) seats themselves curved out from under them.
 *     Fixed by reading the REAL local bounding box off the section's own
 *     live seat list (paddedLocalBBox()/localSeatBBox() below, computed by
 *     inverse-transforming (toLocal()) the exact seats renderSection() just
 *     drew, so it can't drift from what's on screen for ANY geometry --
 *     grid, raked, fanned/arc, rotated, offset, all alike) instead of a
 *     rectangle formula. handleTL/TR/BL/BR, rotateHandleLocal,
 *     handleOffsetLocal, cornerCursor, and the origin/pivot marker
 *     placement all read off this one box now, padded well clear of the
 *     seats (handlePadding()) -- which doubles as the docs/EDITOR.md #4
 *     "handles must never overlap seats" fix, since every handle's
 *     position is now provably outside (or, for the rotate/pivot/origin
 *     markers, pushed further still beyond) that padded box rather than a
 *     heuristic pitch-scaled guess that could land short for an unusual
 *     section.
 *   - Handles are function ICONS now (docs/EDITOR.md #3): each handle's
 *     existing colored dot gets a small inline-SVG glyph on top (rotate =
 *     circular arrow, corners = a diagonal double-arrow, one of two mirrored
 *     base paths per corner pair (see chart_editor.html), rotated by
 *     resizeIconAngle() (== section.rotation) so it turns with the block --
 *     offset = a horizontal shift arrow rotated with the block, move-section
 *     = a 4-way arrow cross, pivot = a crosshair/
 *     target), template-only (see chart_editor.html) plus a couple of small
 *     JS helpers to compute icon transforms. The round-3 invisible hit-zone
 *     circle underneath (and its <title> tooltip) is completely unchanged
 *     -- the icon is purely the decorative layer painted on top.
 *   - Two CORRECTIONS to round 3's own offset decisions, from the same
 *     round of testing: round 3 #8 raised the offset-amount range to a
 *     seat_pitch-scaled ~20+, which turned out to be a misread of the
 *     user's feedback -- offsetRange() is back down to a flat +/-2 (server-
 *     enforced too, dashboard/views.py's chart_editor_save). And round 3
 *     #10 disabled the offset controls outright for arc sections (offset
 *     was a no-op for fanned rows) -- the user wants offset and arc to
 *     compose, so seat_geometry.js's fannedLocal (mirrored in
 *     venues/generation.py's _fanned_local) now adds the same rowXOffset()
 *     term gridOrRakedLocal already applies, and every arc-gated
 *     show/hide/disable on the offset controls (template) is gone.
 *
 * Round 3 (docs/EDITOR.md "Round 3 refinements", real iPad + desktop
 * testing) rebuilds the on-canvas TRANSFORM SYSTEM, which was the actual
 * crux of that feedback:
 *   - A 4th corner handle (handleTL()) was simply missing -- the transform
 *     box's "top-left" corner was drawn from handleOrigin() (the RAW,
 *     unrotated origin_x/origin_y), while the other 3 corners went through
 *     worldFromLocal() (rotation-aware). That mismatch is exactly why the
 *     box "didn't rotate with the section": 3 of its 4 points swung with
 *     `rotation`, one didn't, so the outline sheared instead of turning as
 *     a rigid rectangle. All 4 corners now go through worldFromLocal(),
 *     so the whole frame rotates as one rigid shape (see handleTL/TR/BL/BR
 *     below).
 *   - Touch hit zones: every handle now renders TWO circles -- a small
 *     visible dot (pointer-events: none, purely decorative) and a larger
 *     INVISIBLE hit circle on top (pointer-events: all, fill: transparent
 *     but that's fine -- SVG `pointer-events: all` fires regardless of
 *     paint, unlike the default `visiblePainted`) sized well beyond the
 *     dot and sized up further under `@media (pointer: coarse)` (see
 *     app.css's --chart-editor-hit-scale). setPointerCapture (already
 *     wired in startHandleDrag) keeps a drag glued to that hit circle even
 *     if the pointer slides off it mid-drag.
 *   - Cursors: cornerCursor() below picks nwse-resize/nesw-resize/ns-resize/
 *     ew-resize per corner, ROTATED by the section's current `rotation` (a
 *     corner's natural resize direction visually turns with the block) --
 *     desktop-only affordance, harmless on touch.
 *   - The offset/skew handle moved off the seat block entirely (see
 *     handleOffsetLocal()) instead of sitting at local (row_x_offset, row
 *     height), which routinely landed right on top of a real seat.
 *   - The transform box (corner handles + offset handle) is no longer
 *     hidden outright for arc sections -- only its offset handle is (round-
 *     3 #10: offset is a no-op for fanned rows, see _fanned_local's
 *     docstring -- it never reads row_x_offset), so the resize (pitch)
 *     handles -- and the sidebar's Seat/Row pitch sliders, which were
 *     never actually gated -- stay usable with arc on (round-3 #7).
 *   - Seat labels (round-3 #5) render as <text> children in renderSection(),
 *     hidden via updateLabelVisibility() when zoomed out far enough that
 *     they'd be illegible anyway.
 *   - Enabling/tightening arc no longer translates the section (round-3
 *     #6, "still broken after round 1"): onArcToggle/onArcAmountInput now
 *     run every arc_radius change through seat_geometry.js's
 *     rebalanceOriginForArcChange first -- see that function's doc comment
 *     for the actual root cause (grid's local (0,0) is the front-LEFT seat,
 *     fanned's is front-CENTER, so a bare mode switch with origin held
 *     fixed jumps the block sideways even though a fixed-mode radius
 *     change alone was already jump-free after round 1).
 *
 * Round 2 (docs/EDITOR.md "Round 2 refinements", post-review feedback)
 * adds: paired slider+number inputs (plain HTML/Alpine, template-only --
 * no JS changes needed since both inputs just x-model the same property);
 * a configurable rotation pivot (pivot_mode/pivot_x/pivot_y -- see
 * worldFromLocal/toLocal/the 'rotate'/'move_pivot' onHandleDrag cases, and
 * venues/generation.py's module docstring for the shared contract);
 * responsive handle sizing (CSS-only, static/css/app.css); a background
 * scale grid (plain SVG/Alpine in the template, reactive to `viewBox`, no
 * JS needed); native SVG <title> tooltips on every handle (template-only);
 * a constant seat radius decoupled from seat_pitch/row_pitch (see
 * SEAT_RADIUS below -- the bug was exactly this file's old `radius =
 * seat_pitch * 0.35`); and inline section creation (submitNewSection(),
 * which splices a server-created section straight into `sections`/
 * `sectionOrder` without navigating away -- see ensureSectionGroup() for
 * why section <g> elements are now created here in JS rather than by a
 * Django loop).
 */

const SVG_NS = "http://www.w3.org/2000/svg";

// Round 2 bug fix (docs/EDITOR.md #6): seat_pitch/row_pitch must only
// change the GAPS between seats, never the drawn seat SIZE -- the old
// `radius = seat_pitch * 0.35` tied the two together, so dragging the
// pitch sliders visibly grew/shrank every seat along with the spacing.
// The seat's on-canvas radius is now a plain constant, in the same SVG
// user-space units seat_pitch/row_pitch are (so it still scales normally
// with zoom -- just never with pitch). events/zones.py's
// zone_map_geometry had the exact same coupling for the PNG/PDF export
// (Phase D) and per-performance zone editor; fixed there too, as its own
// constant (SEAT_RADIUS), so the two can't drift.
const SEAT_RADIUS = 0.35;

const PARAM_FIELDS = [
    "origin_x", "origin_y", "rotation", "seat_pitch", "row_pitch", "row_x_offset",
    "offset_mode", "alt_row_seat_delta", "rows", "seats_per_row",
    "numbering_scheme", "row_label_scheme", "pivot_mode", "pivot_x", "pivot_y",
];

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

function seatKey(row, number) {
    return row + "|" + number;
}

// Arc slider mapping ("arc: slider, straight at one end -> tighter curve at
// the other" -- docs/EDITOR.md): amount=0 means arc disabled (grid/raked);
// amount 1..40 maps to a radius that SHRINKS as amount grows, so dragging
// the slider up visibly tightens the curve.
function arcAmountToRadius(amount) {
    return amount <= 0 ? 0 : Math.max(1, (41 - amount) * 2);
}

function arcRadiusToAmount(radius) {
    if (!radius) return 0;
    return clamp(Math.round(41 - radius / 2), 1, 40);
}

function makeSection(raw) {
    const arcRadius = raw.arc_radius || 0;
    return {
        id: raw.id,
        name: raw.name,
        tier: raw.tier,
        color: raw.color,
        editUrl: raw.edit_url,
        reorderUrl: raw.reorder_url,
        origin_x: raw.origin_x,
        origin_y: raw.origin_y,
        rotation: raw.rotation,
        seat_pitch: raw.seat_pitch,
        row_pitch: raw.row_pitch,
        row_x_offset: raw.row_x_offset,
        pivot_mode: raw.pivot_mode || "center",
        pivot_x: raw.pivot_x || 0,
        pivot_y: raw.pivot_y || 0,
        // Round 4 (docs/EDITOR.md #1): no more separate "enabled" flag --
        // arc_amount/arc_radius alone drive the geometry, 0 meaning
        // straight, same as every other slider at its zero value. See this
        // file's header comment.
        arc_radius: arcRadius,
        arc_amount: arcRadiusToAmount(arcRadius),
        offset_mode: raw.offset_mode,
        alt_row_seat_delta: raw.alt_row_seat_delta,
        rows: raw.rows,
        seats_per_row: raw.seats_per_row,
        numbering_scheme: raw.numbering_scheme,
        row_label_scheme: raw.row_label_scheme,
        removedIds: new Set((raw.removed_seats || []).map(([r, n]) => seatKey(r, n))),
        accessibleIds: new Set((raw.accessible_seats || []).map(([r, n]) => seatKey(r, n))),
        seatCount: 0,
    };
}

function chartEditor(config) {
    return {
        ...window.EditorViewport.mixin(),

        chartId: config.chartId,
        saveUrl: config.saveUrl,
        newSectionUrl: config.newSectionUrl,
        sections: {},
        sectionOrder: [],
        selectedId: config.initialSelectedId || null,
        // null | 'origin' (move the whole section) | 'seat_pitch' | 'row_pitch' | 'both'
        // (resize) | 'rotate' | 'offset' | 'move_pivot' (reposition the rotation pivot)
        dragMode: null,
        _dragSectionId: null,
        _dragStart: null,
        _captureTarget: null,
        _capturePointerId: null,
        _bgStart: null,
        _activePointers: new Map(), // pointerId -> {x, y} for background touches
        _pinch: null,               // two-finger pinch-zoom/pan gesture state
        // Round 6 (touch/Safari): drag-the-section-BODY gesture state. Set when
        // a pointerdown lands on a seat (startBodyDrag) so onHandleDrag can tell
        // a section-move drag apart from a clean tap (which opens the popover).
        _bodyDrag: null,
        popover: null, // {sectionId, row, number, accessible, screenX, screenY}
        dirty: false,
        saving: false,
        savedAt: null,
        error: null,
        newSectionOpen: false,
        newSectionForm: { name: "", tier: "" },
        newSectionSaving: false,
        newSectionError: null,
        // Round 3 #11: snap-to-grid, OFF by default -- see snapValue()/
        // maybeSnap() below, applied to POSITION-like handle drags (move
        // section, move pivot, offset) so it "pairs with the background
        // scale grid" (docs/EDITOR.md) rather than snapping spacing values
        // (seat_pitch/row_pitch), which are usually sub-1-unit and would be
        // useless rounded to a whole grid square.
        snapEnabled: false,
        // Round 3 #5: whether seat number labels are currently legible
        // enough to draw -- see updateLabelVisibility()/renderSection().
        labelsVisible: true,

        // -- undo / redo (client-only) ----------------------------------------
        // Two stacks of state snapshots. Scoped to SHAPING edits + seat
        // overrides (params, arc, removed/accessible seats) -- NOT section
        // create or reorder, which touch server rows / the section set and
        // would orphan a DB row if reversed client-side (submitNewSection/
        // reorderSection CLEAR this history instead -- see clearHistory()).
        // Selection IS recorded so undo returns you to the section you were
        // editing, but a pure selection change never lands an entry (material
        // equality ignores selectedId -- see materialKey()). A whole gesture
        // (a slider drag, an on-canvas handle drag, typing a number) collapses
        // into ONE entry: the pre-edit baseline is snapshotted at gesture
        // START -- BEFORE x-model writes the new value, which is the "you
        // can't read the old value inside @input" trap that shaped this whole
        // design -- and pushed at gesture END only if something actually
        // changed (beginCoalesce()/endCoalesce()).
        undoStack: [],
        redoStack: [],
        _pendingBaseline: null,
        _historyLimit: 100,

        init() {
            const raw = JSON.parse(document.getElementById("editor-sections-data").textContent);
            for (const r of raw) {
                this.sections[r.id] = makeSection(r);
                this.sectionOrder.push(r.id);
            }
            if (!this.selectedId || !this.sections[this.selectedId]) {
                this.selectedId = this.sectionOrder[0] || null;
            }
            // See syncViewBoxAttr()'s doc comment: :viewBox="..." can't work
            // on an inline <svg> (HTML parsing lowercases the attribute
            // name before Alpine sees it), so push it imperatively on every
            // reactive change instead.
            this.$watch("viewBox", () => {
                this.syncViewBoxAttr();
                // Round 3 #5: labels degrade gracefully (hide) once the
                // section is zoomed out far enough that they'd render as
                // illegible sub-pixel-ish text -- re-evaluated on every
                // zoom/pan since scaleX changes with the viewBox, not with
                // any per-section param renderSection() already reacts to.
                this.updateLabelVisibility();
            });
            // The selected section's <g> gets a highlight class -- applied
            // imperatively (see refreshGroupClasses()) rather than an
            // Alpine :class binding, since groups themselves are now
            // created imperatively too (ensureSectionGroup(), so a section
            // added inline via submitNewSection() has a group to render
            // into -- see this file's header comment).
            this.$watch("selectedId", () => this.refreshGroupClasses());
            this.$nextTick(() => {
                for (const id of this.sectionOrder) this.renderSection(id);
                this.refreshGroupClasses();
                this.fitAll();
                this.syncViewBoxAttr();
                this.updateLabelVisibility();
            });
            window.addEventListener("beforeunload", (evt) => {
                if (this.dirty) {
                    evt.preventDefault();
                    evt.returnValue = "";
                }
            });

            this.applyHandleRadiusFallback();
        },

        // The transform-box handles size their circles ONLY via the CSS `r`
        // geometry property (app.css: `r: calc(0.3 * var(--chart-editor-
        // handle-scale))`). Older iOS Safari (pre-16) doesn't support `r` as a
        // CSS property at all, so those circles fall back to r=0 -- invisible
        // AND untappable. The dark-iconed resize handles still show their glyph
        // so they looked "there", but the move/rotate/offset handles have WHITE
        // icons meant to sit on a colored dot, so with the dot gone they
        // vanished entirely -- and every handle's invisible hit circle collapsed
        // too, which is the real reason handles "can't be dragged" on the iPad.
        // Setting an explicit `r` ATTRIBUTE is the fix: Safari always honors the
        // presentation attribute, and browsers that DO support the CSS property
        // still override it (CSS geometry props beat presentation attrs), so
        // responsive sizing is unchanged where it works. Attribute values are
        // touch-sized so the fallback is comfortably tappable on the iPad.
        applyHandleRadiusFallback() {
            const svg = this.$refs.svg;
            if (!svg) return;
            const radii = [
                [".chart-editor__handle-hit", 0.9],
                [".chart-editor__handle--resize", 0.34],
                [".chart-editor__handle--offset", 0.3],
                [".chart-editor__handle--rotate", 0.32],
                [".chart-editor__pivot", 0.28],
                [".chart-editor__rotation-pivot", 0.28],
            ];
            for (const [selector, r] of radii) {
                svg.querySelectorAll(selector).forEach((el) => {
                    if (!el.hasAttribute("r")) el.setAttribute("r", r);
                });
            }
        },

        get selected() {
            return this.selectedId != null ? this.sections[this.selectedId] : null;
        },

        get selectedSeatCount() {
            const s = this.selected;
            return s ? s.seatCount : 0;
        },

        get canUndo() {
            return this.undoStack.length > 0;
        },

        get canRedo() {
            return this.redoStack.length > 0;
        },

        // -- geometry / rendering --------------------------------------------

        geomParams(section) {
            return {
                origin_x: section.origin_x,
                origin_y: section.origin_y,
                rotation: section.rotation,
                seat_pitch: section.seat_pitch,
                row_pitch: section.row_pitch,
                row_x_offset: section.row_x_offset,
                // Round 4: section.arc_radius is ALWAYS the section's
                // current, live radius (0 for straight) -- no more
                // "remembered but disabled" indirection through a checkbox
                // flag, so this is just a passthrough now.
                arc_radius: section.arc_radius,
                offset_mode: section.offset_mode,
                alt_row_seat_delta: section.alt_row_seat_delta,
                rows: section.rows,
                seats_per_row: section.seats_per_row,
                numbering_scheme: section.numbering_scheme,
                row_label_scheme: section.row_label_scheme,
                pivot_mode: section.pivot_mode,
                pivot_x: section.pivot_x,
                pivot_y: section.pivot_y,
            };
        },

        computeSeats(section) {
            return window.SeatGeometry.computeSectionSeats(this.geomParams(section), {
                removedIds: section.removedIds,
                accessibleIds: section.accessibleIds,
            });
        },

        // Creates (once) and returns the <g data-section-group> a section's
        // seats render into. Sections present at page load and sections
        // added inline via submitNewSection() both go through this same
        // path now -- see this file's header comment for why group
        // creation moved from a Django template loop into JS.
        // Inserted right before the transform-box <g> (x-ref="transformBox"
        // in the template) so seats always paint UNDER the handles/pivot
        // markers regardless of insertion order (SVG paints in DOM order).
        ensureSectionGroup(id) {
            let g = this.$refs.svg.querySelector(`[data-section-group="${id}"]`);
            if (!g) {
                g = document.createElementNS(SVG_NS, "g");
                g.setAttribute("data-section-group", id);
                this.$refs.svg.insertBefore(g, this.$refs.transformBox);
            }
            return g;
        },

        refreshGroupClasses() {
            for (const id of this.sectionOrder) {
                const g = this.$refs.svg.querySelector(`[data-section-group="${id}"]`);
                if (g) g.setAttribute("class", this.selectedId === id ? "chart-editor__group--selected" : "");
            }
        },

        renderSection(id) {
            const section = this.sections[id];
            if (!section) return;
            const g = this.ensureSectionGroup(id);
            while (g.firstChild) g.removeChild(g.firstChild);

            const seats = this.computeSeats(section).filter((s) => !s.removed);
            section.seatCount = seats.length;
            // Round-2 bug fix (docs/EDITOR.md #6): a constant radius, NOT
            // derived from seat_pitch -- see SEAT_RADIUS's module-level
            // comment. Spacing sliders now only move seats apart/together;
            // they never resize the seat circles themselves.

            for (const seat of seats) {
                const circle = document.createElementNS(SVG_NS, "circle");
                circle.setAttribute("cx", seat.x);
                circle.setAttribute("cy", seat.y);
                circle.setAttribute("r", SEAT_RADIUS);
                circle.setAttribute(
                    "class",
                    "editor-seat" + (seat.accessible ? " editor-seat--accessible" : "")
                );
                circle.setAttribute("fill", section.color);
                circle.dataset.row = seat.row;
                circle.dataset.number = seat.number;
                // Round 6 (touch/Safari, "a different solution"): pointerdown on
                // a seat starts a potential SECTION-BODY drag (startBodyDrag) --
                // dragging the block itself is a finger-sized target that no
                // longer depends on grabbing the ~0.5-unit move handle. A clean
                // tap (no drag past a small threshold) still opens this seat's
                // popover; see startBodyDrag()/onHandleDrag()'s "origin" case/
                // endHandleDrag() for the tap-vs-drag split.
                circle.addEventListener("pointerdown", (evt) => {
                    this.startBodyDrag(id, seat, evt);
                });
                // The popover's `@click.outside="closePopover()"` (template)
                // listens on `document`. For a MOUSE-originated pointer,
                // preventDefault() on 'pointerdown' does NOT suppress the
                // browser's own subsequent native 'click' (that suppression
                // only applies to touch/pen "compatibility" mouse events) --
                // so without this, the very click that OPENS the popover
                // also bubbles to document a moment later and immediately
                // closes it again (confirmed by driving the editor: state
                // was set on pointerdown, then unset again before the next
                // frame). Stopping propagation on 'click' too keeps the
                // opening gesture from closing what it just opened.
                circle.addEventListener("click", (evt) => evt.stopPropagation());
                const title = document.createElementNS(SVG_NS, "title");
                title.textContent =
                    `${section.name} ${seat.row}${seat.number}` + (seat.accessible ? " (accessible)" : "");
                circle.appendChild(title);
                g.appendChild(circle);

                // Round 3 #5: seat numbers, rendered inside the circle --
                // pointer-events: none (app.css) so they never steal the
                // seat's own pointerdown/click, and hidden wholesale via
                // the SVG root's class (see updateLabelVisibility()) once
                // zoomed out far enough to be illegible rather than drawn
                // as unreadable sub-pixel text.
                const label = document.createElementNS(SVG_NS, "text");
                label.setAttribute("x", seat.x);
                label.setAttribute("y", seat.y);
                label.setAttribute("font-size", SEAT_RADIUS * 0.9);
                label.setAttribute("class", "editor-seat-label");
                // Center the number in the seat robustly across browsers:
                // - text-anchor="middle" (an ATTRIBUTE, well supported) handles
                //   horizontal centering.
                // - For vertical centering, do NOT rely on dominant-baseline:
                //   older iOS Safari ignores it (as a CSS property AND, on some
                //   versions, wobbles on the attribute too), which left numbers
                //   sitting high -- the "numbers not centered" report from the
                //   real iPad. Instead anchor at the seat center y and nudge the
                //   text DOWN by ~0.35em with `dy`, which lands a digit's optical
                //   middle on the center on every engine (a numeral's ink sits
                //   ~0.35em above the alphabetic baseline).
                label.setAttribute("text-anchor", "middle");
                label.setAttribute("dy", "0.35em");
                label.textContent = seat.number;
                g.appendChild(label);
            }
        },

        // Round 3 #5: hides seat labels once the seat's ON-SCREEN radius
        // (SEAT_RADIUS converted from SVG user-units to client px via the
        // viewport's current scale -- see editor_viewport.js's
        // contentBox()) drops below a legibility floor, e.g. after
        // zooming/panning out. Toggles one class on the <svg> root (CSS
        // handles the actual hide, static/css/app.css) rather than
        // touching every <text> node, so it's cheap to run on every
        // viewBox change.
        updateLabelVisibility() {
            const svg = this.$refs.svg;
            if (!svg) return;
            const box = this.contentBox();
            const screenSeatPx = box.scaleX ? SEAT_RADIUS / box.scaleX : 0;
            this.labelsVisible = screenSeatPx >= 5.5;
            svg.classList.toggle("chart-editor__svg--labels-hidden", !this.labelsVisible);
        },

        onParamInput(id) {
            this.dirty = true;
            this.renderSection(id);
        },

        // Round 3 #6 ("arc STILL offsets the section" -- the fix, applied):
        // every arc_radius change -- including going to/from 0 -- goes
        // through applyArcRadiusChange() below, which rebalances
        // origin_x/origin_y FIRST so the section's front-center reference
        // point doesn't move (see seat_geometry.js's
        // rebalanceOriginForArcChange / generation.py's matching function
        // for why the 0 <-> nonzero transition -- not a fixed-mode radius
        // change, which was already jump-free after round 1 -- was the
        // actual surviving bug: grid's local (0,0) is the front-LEFT seat,
        // fanned's is front-CENTER). Round 4: onArcAmountInput() is the
        // ONLY caller now -- there's no more separate enable/disable
        // toggle, see this file's header comment and makeSection().

        applyArcRadiusChange(id, newRadius) {
            const s = this.sections[id];
            // Row 0 is always `seats_per_row` seats exactly -- alt_row_seat_delta
            // (ALTERNATING offset_mode) only ever touches ODD row indices
            // (see compute_row_counts / computeRowCounts), so row 0's count
            // is never ragged regardless of offset_mode.
            const [ox, oy] = window.SeatGeometry.rebalanceOriginForArcChange(
                this.geomParams(s), newRadius, s.seats_per_row
            );
            s.origin_x = ox;
            s.origin_y = oy;
            s.arc_radius = newRadius;
        },

        onArcAmountInput(id) {
            const s = this.sections[id];
            this.applyArcRadiusChange(id, arcAmountToRadius(s.arc_amount));
            this.onParamInput(id);
        },

        onOffsetModeInput(id, mode) {
            this.beginCoalesce();
            this.sections[id].offset_mode = mode;
            this.onParamInput(id);
            this.endCoalesce();
        },

        // Round 2 (docs/EDITOR.md #2): the Center/Origin/Custom selector.
        // Switching INTO "custom" seeds pivot_x/pivot_y from whatever the
        // pivot is currently computed as (center or origin) so the pivot
        // marker doesn't jump somewhere unexpected the moment the mode
        // changes -- from there, dragging the marker (onHandleDrag's
        // 'move_pivot' case) moves it exactly where the user drops it.
        onPivotModeInput(id, mode) {
            this.beginCoalesce();
            const s = this.sections[id];
            if (mode === "custom" && s.pivot_mode !== "custom") {
                const [px, py] = window.SeatGeometry.pivotLocal(this.geomParams(s));
                s.pivot_x = px;
                s.pivot_y = py;
            }
            s.pivot_mode = mode;
            this.onParamInput(id);
            this.endCoalesce();
        },

        stepRows(id, delta) {
            this.beginCoalesce();
            const s = this.sections[id];
            s.rows = Math.max(1, s.rows + delta);
            this.onParamInput(id);
            this.endCoalesce();
        },

        stepSeatsPerRow(id, delta) {
            this.beginCoalesce();
            const s = this.sections[id];
            s.seats_per_row = Math.max(1, s.seats_per_row + delta);
            this.onParamInput(id);
            this.endCoalesce();
        },

        // Round 3 #9: alt-row add/drop is a brick-stagger nudge (+1 seat
        // longer / -1 seat shorter on every other row), not a general
        // seat-count control -- clamp to -1/0/+1 client-side (the save
        // endpoint clamps the same way server-side, see dashboard/views.py's
        // chart_editor_save, so a stale/tampered client value can't sneak
        // a bigger delta into storage either).
        stepAltDelta(id, delta) {
            this.beginCoalesce();
            const s = this.sections[id];
            s.alt_row_seat_delta = clamp(s.alt_row_seat_delta + delta, -1, 1);
            this.onParamInput(id);
            this.endCoalesce();
        },

        // Round 4 correction (docs/EDITOR.md): round 3 #8 raised this to a
        // seat_pitch-scaled ~20+, which turned out to be a misread of the
        // user's actual feedback -- capped back down to a flat +/-2 (the
        // slider stays centered/bidirectional, same sign convention as
        // before). `section` is unused now but kept in the signature since
        // the template calls this per-section; server-side enforcement is
        // dashboard/views.py's chart_editor_save.
        offsetRange(section) {
            return 2;
        },

        // -- selection --------------------------------------------------------

        selectSection(id) {
            this.selectedId = id;
        },

        // -- sidebar reordering (Round 2, docs/EDITOR.md #7 feedback) --------
        //
        // `ordering` isn't a manual number field on the New-section form
        // (see SectionForm's docstring) -- these up/down arrows are the
        // whole reordering UI, swapping THIS section with its neighbor in
        // the display list via dashboard_section_reorder (manager-gated,
        // org-/chart-scoped, same as every other section mutation). The
        // server computes the swap and returns the chart's full section-id
        // order; the client just adopts it verbatim rather than trying to
        // replicate the swap logic itself.

        async reorderSection(id, direction) {
            const s = this.sections[id];
            if (!s || !s.reorderUrl) return;
            try {
                const resp = await fetch(s.reorderUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": this.csrfToken() },
                    credentials: "same-origin",
                    body: JSON.stringify({ direction }),
                });
                const data = await resp.json().catch(() => null);
                if (resp.ok && data && data.ok && Array.isArray(data.order)) {
                    this.sectionOrder = data.order;
                    // Reorder isn't undoable (it's a server mutation); wipe
                    // history so an older snapshot's stale order can't be
                    // restored over it. See clearHistory().
                    this.clearHistory();
                }
            } catch (e) {
                // Best-effort -- the sidebar list simply doesn't reorder;
                // no seat/geometry state is at risk either way.
            }
        },

        moveSectionUp(id) {
            this.reorderSection(id, "up");
        },

        moveSectionDown(id) {
            this.reorderSection(id, "down");
        },

        // -- undo / redo ------------------------------------------------------
        //
        // See the undoStack field's comment for the scope + coalescing story.
        // The moving parts: snapshotState() captures a JSON-safe deep copy of
        // all undoable state; beginCoalesce()/endCoalesce() bracket a gesture
        // (baseline snapped at the pre-edit start, pushed at the end only if
        // material state changed); restoreState() re-applies a snapshot and
        // re-renders the seats imperatively (they aren't Alpine-bound).

        // Deep, JSON-safe snapshot of every UNDOABLE piece of editor state:
        // each section's shaping params + arc + the two seat-override Sets
        // (materialized to arrays -- a Set isn't JSON-serializable and we
        // compare snapshots by value), plus the section order and current
        // selection.
        snapshotState() {
            const sections = {};
            for (const id of this.sectionOrder) {
                const s = this.sections[id];
                if (!s) continue;
                const params = {};
                for (const f of PARAM_FIELDS) params[f] = s[f];
                params.arc_radius = s.arc_radius;
                params.arc_amount = s.arc_amount;
                params.removedIds = [...s.removedIds];
                params.accessibleIds = [...s.accessibleIds];
                sections[id] = params;
            }
            return {
                sections,
                sectionOrder: [...this.sectionOrder],
                selectedId: this.selectedId,
            };
        },

        // The part of a snapshot that decides whether anything CHANGED --
        // deliberately EXCLUDES selectedId, so tapping between sections (which
        // snapshotState records so undo can restore it) never lands a no-op
        // entry on the stack; only real shape/seat edits do.
        materialKey(snap) {
            return JSON.stringify({ sections: snap.sections, sectionOrder: snap.sectionOrder });
        },

        // Re-apply a snapshot to live state, then RE-RENDER imperatively --
        // seats are drawn by hand (not Alpine-bound), so nothing repaints on
        // its own. Rebuilds every section's <circle>s (renderSection), drops
        // any orphan section <g> no longer in the order, and re-highlights the
        // selected group.
        restoreState(snap) {
            for (const id of snap.sectionOrder) {
                const s = this.sections[id];
                const p = snap.sections[id];
                if (!s || !p) continue;
                for (const f of PARAM_FIELDS) s[f] = p[f];
                s.arc_radius = p.arc_radius;
                s.arc_amount = p.arc_amount;
                s.removedIds = new Set(p.removedIds);
                s.accessibleIds = new Set(p.accessibleIds);
            }
            this.sectionOrder = [...snap.sectionOrder];
            if (snap.selectedId != null && this.sections[snap.selectedId]) {
                this.selectedId = snap.selectedId;
            } else if (!this.sections[this.selectedId]) {
                this.selectedId = this.sectionOrder[0] || null;
            }
            this.dirty = true;
            this.closePopover();
            this.pruneOrphanGroups();
            for (const id of this.sectionOrder) this.renderSection(id);
            this.refreshGroupClasses();
        },

        // Remove any section <g> whose id is no longer in sectionOrder. Create
        // currently clears history so an undo can't cross back over an
        // inline-created section, but this keeps restoreState() correct even
        // if that ever changes.
        pruneOrphanGroups() {
            const keep = new Set(this.sectionOrder.map(String));
            this.$refs.svg.querySelectorAll("[data-section-group]").forEach((g) => {
                if (!keep.has(g.getAttribute("data-section-group"))) g.remove();
            });
        },

        // Start of a coalesced edit gesture: snapshot the PRE-edit baseline
        // now, BEFORE any x-model write (the trap this whole design routes
        // around). Defensively finalizes a prior gesture whose end event never
        // fired (a field focused then abandoned without a change, a slider
        // grabbed but released without moving) so a stale baseline can't leak
        // into this one.
        beginCoalesce() {
            if (this._pendingBaseline) this.endCoalesce();
            this._pendingBaseline = this.snapshotState();
        },

        // End of a coalesced edit gesture: push the baseline onto the undo
        // stack IFF the gesture actually changed something material, and drop
        // the redo stack. A gesture that changed nothing (a tap, a slider
        // grabbed but not moved, a field focused but not edited) leaves no
        // trace.
        endCoalesce() {
            const baseline = this._pendingBaseline;
            this._pendingBaseline = null;
            if (!baseline) return;
            if (this.materialKey(baseline) === this.materialKey(this.snapshotState())) return;
            this.undoStack.push(baseline);
            if (this.undoStack.length > this._historyLimit) this.undoStack.shift();
            this.redoStack = [];
        },

        // Structural changes (inline section create, reorder) can't be safely
        // reversed client-side -- undo scope is shaping + seat overrides only
        // -- so they WIPE the history rather than leave entries that would
        // restore a stale section set/order (a pre-create snapshot is missing
        // the new section; undoing to it would orphan a real DB row).
        clearHistory() {
            this.undoStack = [];
            this.redoStack = [];
            this._pendingBaseline = null;
        },

        undo() {
            this.endCoalesce(); // finalize any in-flight gesture first
            if (!this.undoStack.length) return;
            const current = this.snapshotState();
            const prev = this.undoStack.pop();
            this.redoStack.push(current);
            this.restoreState(prev);
        },

        redo() {
            this.endCoalesce();
            if (!this.redoStack.length) return;
            const current = this.snapshotState();
            const next = this.redoStack.pop();
            this.undoStack.push(current);
            this.restoreState(next);
        },

        // Cmd/Ctrl+Z = undo, Cmd/Ctrl+Shift+Z or Ctrl+Y = redo. Deliberately
        // INERT while a form field (the number/text inputs, selects) has focus
        // so the browser's native text-undo keeps working inside those -- the
        // toolbar Undo/Redo buttons (required for touch, which has no
        // keyboard) cover editing-a-field cases.
        onEditorKeydown(evt) {
            if (!(evt.metaKey || evt.ctrlKey)) return;
            const key = (evt.key || "").toLowerCase();
            if (key !== "z" && key !== "y") return;
            const t = evt.target;
            const tag = t && t.tagName ? t.tagName.toLowerCase() : "";
            if (tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable)) {
                return;
            }
            if (key === "y" || (key === "z" && evt.shiftKey)) {
                evt.preventDefault();
                this.redo();
            } else if (key === "z") {
                evt.preventDefault();
                this.undo();
            }
        },

        // -- transform box: local <-> world, handle positions -----------------

        // Naive rectangle from the section's SHAPE params -- correct for a
        // plain grid/raked block, but does NOT account for arc's curved
        // local geometry (see localSeatBBox() below, which superseded this
        // for every handle-placement purpose in Round 4). Kept only as
        // localSeatBBox()'s degenerate-section fallback (every seat
        // removed) and by pivotLocal-style callers that genuinely want the
        // shape-only box (none remain in this file, but seat_geometry.js's
        // pivotLocal computes the same thing server-side-mirrored, so this
        // stays as a readable reference for that formula).
        localWH(section) {
            const w = Math.max(0, section.seats_per_row - 1) * section.seat_pitch;
            const h = Math.max(0, section.rows - 1) * section.row_pitch;
            return [w, h];
        },

        // Round 4 (docs/EDITOR.md #2, "THE BIG ONE"): the REAL local
        // (pre-rotation) bounding box of `section`'s actual current seats
        // -- not the naive `localWH()` rectangle, which silently assumes a
        // grid/raked layout and drifts from the true seat positions the
        // moment arc bends a row's seats off that assumed rectangle.
        // Computed by inverse-transforming (toLocal()) the EXACT world
        // coordinates renderSection() just drew (via computeSeats()), so
        // it's provably the same seats that are on screen -- correct for
        // grid, raked, fanned/arc, rotated, and offset alike, with no
        // separate geometry-specific formula to keep in sync. Every
        // transform-box handle and the origin/pivot markers are placed off
        // this one box (paddedLocalBBox() below) instead of ad hoc
        // pitch-scaled heuristics.
        localSeatBBox(section) {
            const seats = this.computeSeats(section).filter((s) => !s.removed);
            if (!seats.length) {
                // Degenerate case (every seat removed, or 0 rows/seats) --
                // fall back to the shape-only rectangle so the transform
                // box still has something sane to draw.
                const [w, h] = this.localWH(section);
                return { minX: 0, minY: 0, maxX: w, maxY: h };
            }
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
            for (const seat of seats) {
                const [lx, ly] = this.toLocal(section, seat.x, seat.y);
                minX = Math.min(minX, lx);
                maxX = Math.max(maxX, lx);
                minY = Math.min(minY, ly);
                maxY = Math.max(maxY, ly);
            }
            return { minX, minY, maxX, maxY };
        },

        // Round 4 (docs/EDITOR.md #4, "handles must never overlap seats"):
        // how far outward (in LOCAL units, same scale as seat_pitch/
        // row_pitch) every handle clears the seat block. Every corner
        // handle sits EXACTLY `handlePadding()` away (in at least one axis)
        // from the nearest seat by construction (see paddedLocalBBox()),
        // and comfortably exceeds the largest hit-zone radius a handle can
        // have (app.css's --chart-editor-hit-scale, up to 0.55 * 1.8 =
        // 0.99) plus the seat's own drawn radius (SEAT_RADIUS, 0.35) --
        // 2.2 alone clears both with room to spare; the seat_pitch/
        // row_pitch terms grow the margin further for widely-spaced
        // sections so it still reads as "clearly outside the block" rather
        // than just barely.
        handlePadding(section) {
            return Math.max(2.2, (section.seat_pitch || 0) * 0.9, (section.row_pitch || 0) * 0.9);
        },

        // localSeatBBox() padded by handlePadding() on all 4 sides -- the
        // single source of truth every corner/rotate/offset handle and the
        // origin/pivot markers are positioned from (worldFromLocal() of one
        // of this box's corners/edges), so all of them are provably clear
        // of every seat for ANY geometry, and none of them can drift from
        // the seats arc/rotation/offset actually draw.
        paddedLocalBBox(section) {
            const box = this.localSeatBBox(section);
            const pad = this.handlePadding(section);
            return {
                minX: box.minX - pad, maxX: box.maxX + pad,
                minY: box.minY - pad, maxY: box.maxY + pad,
            };
        },

        // World position of a LOCAL (pre-rotation) point in `section`'s
        // frame -- rotates around the section's CONFIGURED pivot (Round 2,
        // docs/EDITOR.md #2 -- see seat_geometry.js's pivotLocal/seatXY and
        // venues/generation.py's module docstring for the shared contract),
        // not always the origin corner like before. Every transform-box
        // handle (resize/offset/rotate) is expressed as a local point and
        // goes through this, so they all correctly swing around whichever
        // pivot is selected.
        worldFromLocal(section, lx, ly) {
            const [px, py] = window.SeatGeometry.pivotLocal(this.geomParams(section));
            const [rx, ry] = window.SeatGeometry.rotate(lx - px, ly - py, section.rotation);
            return [section.origin_x + px + rx, section.origin_y + py + ry];
        },

        // Inverse of worldFromLocal -- used by the resize/offset drag math
        // to turn a pointer's world position back into the section's local
        // (pre-rotation) frame, regardless of the current pivot/rotation.
        toLocal(section, worldX, worldY) {
            const [px, py] = window.SeatGeometry.pivotLocal(this.geomParams(section));
            const dx = worldX - section.origin_x - px;
            const dy = worldY - section.origin_y - py;
            const [rx, ry] = window.SeatGeometry.rotate(dx, dy, -section.rotation);
            return [rx + px, ry + py];
        },

        // World (x, y) of the section's ORIGIN (origin_x, origin_y) -- the
        // translate-only placement anchor, distinct from the rotation pivot
        // since Round 2 (see pivotWorldXY() below). Unlike a local (lx, ly)
        // point, the origin itself never needs `worldFromLocal`'s rotation:
        // it's the thing everything else is rotated/translated relative to.
        handleOrigin() {
            const s = this.selected;
            return s ? [s.origin_x, s.origin_y] : [0, 0];
        },

        // World (x, y) of the section's CONFIGURED ROTATION PIVOT (Round 2)
        // -- origin + pivotLocal, invariant under rotation by construction
        // (see seat_geometry.js's pivotXY doc comment).
        pivotWorldXY() {
            const s = this.selected;
            if (!s) return [0, 0];
            return window.SeatGeometry.pivotXY(this.geomParams(s));
        },

        // The origin ("move section") MARKER is drawn/clickable near the
        // padded box's TOP-LEFT corner -- not exactly on the TRUE origin
        // (handleOrigin()), and not exactly on TL either (a further
        // handlePadding()-sized step beyond it). A thin connector line ties
        // the marker back to the real origin point (handleOrigin(), a
        // small non-interactive dot) so it stays unambiguous even when the
        // two are far apart.
        //
        // Round 4 (docs/EDITOR.md #2/#4): previously this was a plain
        // pitch-scaled constant delta off the RAW origin, deliberately NOT
        // routed through worldFromLocal/rotation -- fine for a plain grid,
        // but for arc/rotated sections a fixed delta can't reliably clear
        // the actual (curved/turned) seat block, which is exactly the
        // "marker drifts off the curved seats" bug. Routing it through
        // worldFromLocal() off the padded box's own TL corner instead
        // guarantees clearance for ANY geometry (same box every other
        // handle uses) while still rotating naturally with the block.
        originMarkerXY() {
            // Round 5 (declutter): the move-section marker now hugs the LEFT
            // edge at the box's mid-height, not flung diagonally out past the
            // top-left corner. At mid-height it clears the corner resize
            // handles (top/bottom) and sits just outside the seats, so it
            // reads as part of the frame and needs NO long dashed leader line
            // back to the block -- those cross-canvas connector lines were the
            // "stray lines on controls" the user flagged. Drag math is
            // unchanged (onHandleDrag's 'origin' case is a pure pointer delta,
            // independent of where this marker is drawn).
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            const extra = this.handlePadding(s) * 0.35;
            const cy = (b.minY + b.maxY) / 2;
            return this.worldFromLocal(s, b.minX - extra, cy);
        },

        // The ROTATION PIVOT marker (Round 2, docs/EDITOR.md #2): same
        // treatment as the origin marker above, but anchored beyond the
        // padded box's OPPOSITE (bottom-right) corner instead of top-left,
        // so the two draggable markers/connectors never sit on top of each
        // other -- including when pivot_mode is ORIGIN (pivot === origin
        // exactly) or a CENTER pivot lands deep inside the seat block
        // (which the OLD fixed-delta-off-the-true-pivot offset did NOT
        // reliably clear -- a small delta off a pivot that's already inside
        // the block can still land on a seat; anchoring off the padded
        // box's own corner instead guarantees it's outside the block
        // regardless of where the true pivot sits). Dragging this marker
        // always sets pivot_mode to CUSTOM (see onHandleDrag's 'move_pivot'
        // case) -- it's the one control that works regardless of which
        // mode is currently selected in the Center/Origin/Custom toggle.
        rotationPivotMarkerXY() {
            // Round 5 (declutter): mirror of originMarkerXY -- hugs the RIGHT
            // edge at mid-height, opposite the move marker, so the two never
            // collide and neither needs a long connector line. The true pivot
            // (pivotWorldXY, possibly deep inside the block at a CENTER pivot)
            // is still shown by a small non-interactive dot; this draggable
            // marker just lives out on the frame. 'move_pivot' drag math reads
            // the pointer's world position, not this marker, so it's unchanged.
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            const extra = this.handlePadding(s) * 0.35;
            const cy = (b.minY + b.maxY) / 2;
            return this.worldFromLocal(s, b.maxX + extra, cy);
        },

        // Round 5 (declutter): start point of the short rotate-handle stem --
        // the box's TOP-edge center, NOT the pivot (which for a CENTER pivot
        // sits mid-block, making the old stem a long line straight through the
        // seats). Drawn to handleRotate() just above it, so the stem is a
        // short tick outside the frame instead of a cross-canvas "stray line."
        rotateStemStart() {
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            const cx = (b.minX + b.maxX) / 2;
            return this.worldFromLocal(s, cx, b.minY);
        },

        // Round 3 #1/#2, Round 4 #2: the transform box's 4 corners, ALL
        // read off paddedLocalBBox() (the REAL bounding box of the
        // section's live seats, padded clear of them) and run through the
        // same worldFromLocal() pipeline -- so the box both rotates as a
        // rigid shape with the section (round 3's fix) AND tightly wraps
        // the actual seats for grid/raked/fanned/arc alike, padded well
        // clear of them (round 4's fix), instead of a naive
        // seats_per_row/rows rectangle that only happened to match a plain
        // grid.
        handleTL() {
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            return this.worldFromLocal(s, b.minX, b.minY);
        },

        handleTR() {
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            return this.worldFromLocal(s, b.maxX, b.minY);
        },

        handleBL() {
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            return this.worldFromLocal(s, b.minX, b.maxY);
        },

        handleBR() {
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            return this.worldFromLocal(s, b.maxX, b.maxY);
        },

        // Round 3 #4: which resize cursor a corner shows on hover, ROTATED
        // by the section's current `rotation` so it visually matches the
        // block's actual on-screen orientation (a corner that reads as
        // nwse-resize at rotation=0 may read as ns-resize once the block is
        // turned 45deg) -- bucketed into the 4 discrete cursors CSS offers
        // (native CSS can't animate a resize cursor to an arbitrary angle).
        // `cornerLocalX/Y` is the corner's LOCAL point -- Round 4: now one
        // of paddedLocalBBox()'s 4 corners (passed in from the template,
        // see chart_editor.html) rather than a (0,0)/(w,h) rectangle
        // corner; direction is measured from the PADDED BOX's own local
        // center, which works the same way for an arc's non-rectangular
        // extent as it did for a plain grid's rectangle.
        cornerCursor(cornerLocalX, cornerLocalY) {
            const s = this.selected;
            if (!s) return "nwse-resize";
            const b = this.paddedLocalBBox(s);
            const cx = (b.minX + b.maxX) / 2;
            const cy = (b.minY + b.maxY) / 2;
            const [dx, dy] = window.SeatGeometry.rotate(cornerLocalX - cx, cornerLocalY - cy, s.rotation);
            let angle = (Math.atan2(dy, dx) * 180) / Math.PI;
            angle = ((angle % 180) + 180) % 180; // 0..180 -- opposite corners share a cursor
            if (angle < 22.5 || angle >= 157.5) return "ew-resize";
            if (angle < 67.5) return "nwse-resize";
            if (angle < 112.5) return "ns-resize";
            return "nesw-resize";
        },

        // Thin per-corner wrappers around cornerCursor() -- guard against
        // `selected` being null themselves (paddedLocalBBox() would throw
        // on a null section) so the template can call them directly without
        // a `selected ? ... : 'fallback'` ternary at every call site.
        cornerCursorTL() {
            const s = this.selected;
            if (!s) return "nwse-resize";
            const b = this.paddedLocalBBox(s);
            return this.cornerCursor(b.minX, b.minY);
        },

        cornerCursorTR() {
            const s = this.selected;
            if (!s) return "nesw-resize";
            const b = this.paddedLocalBBox(s);
            return this.cornerCursor(b.maxX, b.minY);
        },

        cornerCursorBL() {
            const s = this.selected;
            if (!s) return "nesw-resize";
            const b = this.paddedLocalBBox(s);
            return this.cornerCursor(b.minX, b.maxY);
        },

        cornerCursorBR() {
            const s = this.selected;
            if (!s) return "nwse-resize";
            const b = this.paddedLocalBBox(s);
            return this.cornerCursor(b.maxX, b.maxY);
        },

        // Round 4: centered over the padded box's TOP edge (was the naive
        // rectangle's local x = w/2) and pushed further clear above it
        // (was a w-scaled distance that didn't account for arc's actual
        // local extent) -- correct above the section's true front-center
        // for a grid OR a curved arc block alike.
        rotateHandleLocal(s) {
            const b = this.paddedLocalBBox(s);
            const cx = (b.minX + b.maxX) / 2;
            const extra = this.handlePadding(s) * 0.6;
            return [cx, b.minY - extra];
        },

        handleRotate() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [lx, ly] = this.rotateHandleLocal(s);
            return this.worldFromLocal(s, lx, ly);
        },

        // Round 3 #4, Round 4 #2/#4: the offset/skew handle's LOCAL
        // position, pushed clear of the seat block below the padded box's
        // BOTTOM edge (was a naive rectangle + row_pitch-scaled margin,
        // which for an arc section didn't track the block's real curved
        // extent) instead of sitting AT local (row_x_offset, <a row's y>),
        // which routinely landed right on top of a real seat. Local X still
        // tracks the current offset amount 1:1 (so the handle visibly
        // slides as row_x_offset changes, same affordance as before),
        // clamped to the padded box's own x range (plus a little extra) so
        // it can't wander arbitrarily far from the block -- only Y moved.
        // Round 4 correction: offset now composes with arc (see this
        // file's header comment), so this handle is no longer arc-gated.
        handleOffsetLocal(s) {
            const b = this.paddedLocalBBox(s);
            const extra = this.handlePadding(s) * 0.6;
            const lx =
                s.offset_mode === "alternating"
                    ? s.row_x_offset
                    : Math.max(0, s.rows - 1) * s.row_x_offset;
            return [clamp(lx, b.minX - extra, b.maxX + extra), b.maxY + extra];
        },

        handleOffset() {
            const s = this.selected;
            if (!s) return [0, 0];
            return this.worldFromLocal(s, ...this.handleOffsetLocal(s));
        },

        // Round 5 (declutter): start of the offset handle's short stem -- the
        // bottom edge directly ABOVE the handle (same local x), so the tick is
        // a short vertical line down to the handle rather than a diagonal from
        // the bottom-left corner across to it.
        offsetStemStart() {
            const s = this.selected;
            if (!s) return [0, 0];
            const b = this.paddedLocalBBox(s);
            const [lx] = this.handleOffsetLocal(s);
            return this.worldFromLocal(s, lx, b.maxY);
        },

        // Round 4 (docs/EDITOR.md #3): rotation (degrees) for a corner's
        // diagonal resize-arrow ICON -- just `section.rotation`, NOT an
        // absolute angle computed from the corner's position relative to
        // the box center. The two base icon PATHS in chart_editor.html
        // already encode each corner-pair's own diagonal at rotation=0 (TL/
        // BR share a NW-SE path, TR/BL share a NE-SW path, mirrored) -- an
        // EARLIER version of this computed the corner's true
        // atan2(dy, dx) outward angle instead, which double-counted the
        // diagonal: that angle is close to +/-45/135 deg already (a corner
        // IS roughly diagonal from the box center by definition), so
        // rotating an already-diagonal path by another ~45deg swung it
        // toward vertical/horizontal instead -- confirmed by driving the
        // editor and screenshotting a near-vertical glyph where a diagonal
        // was expected. Plain `section.rotation` is exactly the delta the
        // glyph needs: at rotation=0 the base path is already correct, and
        // as the block turns, every corner's TRUE diagonal direction turns
        // by exactly `rotation` too (a rigid rectangle's corners keep the
        // same relative angles to each other under rotation), so applying
        // that same delta to the icon keeps it aligned with zero extra
        // math.
        resizeIconAngle() {
            const s = this.selected;
            return s ? s.rotation : 0;
        },

        // Round 4 (docs/EDITOR.md #3): translate+rotate transform string
        // for a handle icon <g> -- rotation is optional (defaults to 0 for
        // the icons that don't need to track corner direction/block tilt,
        // e.g. the rotate/move/pivot glyphs).
        handleIconTransform(worldXY, angleDeg) {
            return "translate(" + worldXY[0] + "," + worldXY[1] + ") rotate(" + (angleDeg || 0) + ")";
        },

        // Round 3 #11: snap-to-grid, OFF by default (this.snapEnabled).
        // Snaps to whole units -- the SAME 1-unit spacing as the minor
        // background grid (static/css/app.css's #editor-grid-minor /
        // template's <pattern>), so a snapped drag visibly lines up with
        // the grid lines staff can see. Applied to POSITION values (move
        // section, move pivot, offset amount) below, NOT seat_pitch/
        // row_pitch -- those are spacing values usually well under 1 unit
        // (a typical seat_pitch is 0.2-5), so rounding them to a whole grid
        // unit would make fine spacing control unusable.
        snapValue(value) {
            return this.snapEnabled ? Math.round(value) : value;
        },

        // -- transform box: dragging --------------------------------------

        // Round 6 (touch/Safari, "a different solution for touch screens"):
        // start a potential section-BODY drag from a seat pointerdown. Moving
        // a section by dragging the block itself is a forgiving, finger-sized
        // gesture -- unlike hunting for the ~0.5-unit move handle -- and it
        // reuses the exact same "origin" drag path (onHandleDrag) the handle
        // uses, snap-to-grid included. A clean tap (pointer never passes the
        // ~6px threshold) opens the seat popover instead (endHandleDrag).
        startBodyDrag(id, seat, evt) {
            evt.preventDefault();
            evt.stopPropagation();
            this.selectSection(id);
            const s = this.sections[id];
            if (!s) return;
            // Bracket the whole gesture into one undo entry. A pure TAP (never
            // passes the drag threshold) changes nothing material, so
            // endHandleDrag()'s endCoalesce() discards this baseline -- only a
            // real move lands an entry.
            this.beginCoalesce();
            this.dragMode = "origin";
            this._dragSectionId = id;
            this._dragStart = {
                pointer: this.clientToViewBox(evt.clientX, evt.clientY),
                originX: s.origin_x,
                originY: s.origin_y,
            };
            this._bodyDrag = {
                sectionId: id,
                seat,
                moved: false,
                pointerId: evt.pointerId,
                startClientX: evt.clientX,
                startClientY: evt.clientY,
            };
            // Pointer capture is DEFERRED until the gesture actually crosses the
            // drag threshold (onHandleDrag's "origin" case), NOT taken here on
            // pointerdown. Capturing on the <svg> root retargets the trailing
            // `click` away from the seat circle, which bypasses the circle's
            // click-stopPropagation guard and lets the popover's @click.outside
            // immediately close a popover a pure TAP is about to open. A tap
            // therefore never captures; only a real drag does (see below).
            this._captureTarget = null;
            this._capturePointerId = null;
        },

        startHandleDrag(mode, evt) {
            if (!this.selected) return;
            evt.preventDefault();
            evt.stopPropagation();
            // Bracket the whole handle drag into one undo entry (pre-edit
            // baseline snapped now, pushed in endHandleDrag() if it moved).
            this.beginCoalesce();
            this.dragMode = mode;
            this._dragSectionId = this.selectedId;
            // Stash the section's origin at gesture start so the "origin"
            // (move-section) drag can compute an ABSOLUTE new position from
            // base + total pointer delta rather than accumulating per-event
            // deltas -- see onHandleDrag()'s "origin" case for why the old
            // incremental form broke snap-to-grid.
            this._dragStart = {
                pointer: this.clientToViewBox(evt.clientX, evt.clientY),
                originX: this.selected.origin_x,
                originY: this.selected.origin_y,
            };
            // Pointer capture glues the whole drag to this handle even as a
            // fingertip slides off the small hit target -- essential on touch.
            // Guarded in try/catch: iOS Safari can throw here (e.g. the pointer
            // was already released), and a throw at this point would abort the
            // whole gesture -- exactly the "can't drag things" failure. Stash
            // the target + id so endHandleDrag() can release capture explicitly
            // instead of trusting the implicit release, which Safari sometimes
            // skips (leaving the next tap captured by a stale handle).
            this._captureTarget = evt.target;
            this._capturePointerId = evt.pointerId;
            try {
                if (evt.target.setPointerCapture) evt.target.setPointerCapture(evt.pointerId);
            } catch (e) {
                /* capture is best-effort; the window-level move/up handlers
                   still drive the drag without it. */
            }
        },

        onCanvasPointerMove(evt) {
            if (this.dragMode) {
                this.onHandleDrag(evt);
                return;
            }
            if (this._activePointers.has(evt.pointerId)) {
                this._activePointers.set(evt.pointerId, { x: evt.clientX, y: evt.clientY });
            }
            if (this._pinch && this._activePointers.size >= 2) {
                this.onPinchMove();
            } else if (this.isPanning()) {
                this.onPanMove(evt);
            }
        },

        onHandleDrag(evt) {
            const s = this.sections[this._dragSectionId];
            if (!s) return;
            evt.preventDefault();
            const [px, py] = this.clientToViewBox(evt.clientX, evt.clientY);

            if (this.dragMode === "origin") {
                // Translate the whole section -- a pure pointer-delta drag,
                // unaffected by rotation/pivot (see handleOrigin()'s doc
                // comment: origin_x/origin_y is the placement anchor, not a
                // rotated local point).
                //
                // Round 6 (snap fix, "the grid snapping doesn't conform to a
                // grid"): compute the new origin from the drag-START origin +
                // the FULL pointer delta, then snap ONCE. The old code added
                // the tiny per-event delta to the live origin, snapped, and
                // reset the reference every event -- so with snap on, each
                // sub-1-unit per-event delta rounded straight back to the
                // current origin and the section never actually moved onto a
                // grid line. Absolute base + total delta snaps cleanly to
                // whole-unit grid squares (default seat/row pitch is 1.0, so
                // the seats themselves line up with the minor grid) and still
                // moves smoothly (identity snap) when snap is off.
                //
                // For a BODY drag (started on a seat, _bodyDrag set) hold the
                // section still until the gesture clearly passes the tap
                // threshold, so a tap that wobbles a few px doesn't nudge the
                // section (and mark it dirty) before opening the popover.
                if (this._bodyDrag && !this._bodyDrag.moved) {
                    const moved = Math.hypot(
                        evt.clientX - this._bodyDrag.startClientX,
                        evt.clientY - this._bodyDrag.startClientY
                    );
                    if (moved > 6) {
                        this._bodyDrag.moved = true;
                        // Threshold crossed -> this is a real drag, not a tap.
                        // NOW take pointer capture on the STABLE <svg> root so
                        // the rest of the drag stays glued to the finger even
                        // as renderSection() rebuilds the seat <circle> under
                        // it (a capture on the circle would drop the instant it
                        // first re-renders, and iOS Safari would then stop
                        // delivering pointermove -- the classic "can't drag on
                        // touch"). Deferred to here so a pure tap never captures
                        // (see startBodyDrag). The window-level move/up handlers
                        // still drive the drag even if capture is refused.
                        const svg = this.$refs.svg;
                        this._captureTarget = svg;
                        this._capturePointerId = this._bodyDrag.pointerId;
                        try {
                            if (svg && svg.setPointerCapture) svg.setPointerCapture(this._bodyDrag.pointerId);
                        } catch (e) {
                            /* capture is best-effort */
                        }
                    }
                }
                if (!this._bodyDrag || this._bodyDrag.moved) {
                    const [startPx, startPy] = this._dragStart.pointer;
                    s.origin_x = this.snapValue(this._dragStart.originX + (px - startPx));
                    s.origin_y = this.snapValue(this._dragStart.originY + (py - startPy));
                    this.dirty = true;
                    this.renderSection(this._dragSectionId);
                }
                return;
            } else if (this.dragMode === "move_pivot") {
                // Reposition the ROTATION PIVOT (Round 2, docs/EDITOR.md
                // #2): a pivot's world position is always origin +
                // pivot_local with NO rotation applied (see
                // seat_geometry.js's pivotXY doc comment), so converting the
                // dropped world point back to local is just subtraction --
                // no toLocal()/rotation-inversion needed. Always switches
                // pivot_mode to CUSTOM, regardless of what it was before.
                // Round 3 #11: snapped (in WORLD space, same as origin
                // above) when snap-to-grid is on.
                s.pivot_mode = "custom";
                s.pivot_x = this.snapValue(px) - s.origin_x;
                s.pivot_y = this.snapValue(py) - s.origin_y;
            } else if (this.dragMode === "seat_pitch" || this.dragMode === "both") {
                const denom = Math.max(1, s.seats_per_row - 1);
                const [lx] = this.toLocal(s, px, py);
                s.seat_pitch = clamp(lx / denom, 0.2, 60);
            }
            if (this.dragMode === "row_pitch" || this.dragMode === "both") {
                const denom = Math.max(1, s.rows - 1);
                const [, ly] = this.toLocal(s, px, py);
                s.row_pitch = clamp(ly / denom, 0.2, 60);
            }
            if (this.dragMode === "rotate") {
                // Angle is measured relative to the section's CONFIGURED
                // PIVOT (Round 2), not always the origin -- see
                // pivotWorldXY()/pivotLocal so the rotate handle tracks the
                // cursor correctly no matter which pivot is selected.
                const [pivotWX, pivotWY] = this.pivotWorldXY();
                const pointerVec = [px - pivotWX, py - pivotWY];
                const pointerAngle = Math.atan2(pointerVec[1], pointerVec[0]);
                const [hx, hy] = this.rotateHandleLocal(s);
                const [pvx, pvy] = window.SeatGeometry.pivotLocal(this.geomParams(s));
                const handleAngle = Math.atan2(hy - pvy, hx - pvx);
                const deg = ((pointerAngle - handleAngle) * 180) / Math.PI;
                s.rotation = clamp(deg, -45, 45);
            }
            if (this.dragMode === "offset") {
                // Round 3 #11 / Round 4 correction: clamp matches the
                // sidebar slider's range (offsetRange(), a flat +/-2 as of
                // round 4) so dragging the on-canvas handle can't exceed
                // what the number input allows, and snaps to the grid same
                // as the other position-like handles when snap-to-grid is
                // on. Works the same with arc on now too (round 4).
                const bound = this.offsetRange(s);
                const [lx] = this.toLocal(s, px, py);
                if (s.offset_mode === "alternating") {
                    s.row_x_offset = this.snapValue(clamp(lx, -bound, bound));
                } else {
                    const denom = Math.max(1, s.rows - 1);
                    s.row_x_offset = this.snapValue(clamp(lx / denom, -bound, bound));
                }
            }
            this.dirty = true;
            this.renderSection(this._dragSectionId);
        },

        endHandleDrag(evt) {
            // Close the undo bracket opened in startHandleDrag()/startBodyDrag()
            // -- pushes an entry only if the drag actually changed something
            // (so a tap-to-open-popover leaves no trace).
            this.endCoalesce();
            if (this._captureTarget && this._capturePointerId != null) {
                try {
                    if (this._captureTarget.releasePointerCapture)
                        this._captureTarget.releasePointerCapture(this._capturePointerId);
                } catch (e) {
                    /* already released (e.g. pointercancel got here first) */
                }
            }
            // Round 6: a section-body gesture that never passed the drag
            // threshold is a plain tap -- open that seat's popover (anchored at
            // the original touch point). A pointercancel (system-interrupted
            // gesture) is never a tap, so it opens nothing.
            const body = this._bodyDrag;
            this._captureTarget = null;
            this._capturePointerId = null;
            this.dragMode = null;
            this._dragSectionId = null;
            this._dragStart = null;
            this._bodyDrag = null;
            if (body && !body.moved && evt && evt.type !== "pointercancel") {
                this.openPopover(body.sectionId, body.seat, {
                    clientX: body.startClientX,
                    clientY: body.startClientY,
                });
            }
        },

        // -- background: pan + click-to-deselect ------------------------------

        onCanvasPointerDown(evt) {
            this.popover = null;
            this._activePointers.set(evt.pointerId, { x: evt.clientX, y: evt.clientY });
            if (this._activePointers.size >= 2) {
                // Second finger down -> switch from single-finger pan to a
                // two-finger pinch-zoom/pan. Native pinch is disabled by the
                // locked viewport meta (templates/base.html), so the canvas
                // has to implement its own -- otherwise there's no way to zoom
                // on the iPad at all (wheel-zoom is desktop-only). Cancel any
                // in-progress single-finger pan/tap first.
                this.endPan();
                this._bgStart = null;
                this.beginPinch();
            } else {
                this._bgStart = { x: evt.clientX, y: evt.clientY };
                this.startPan(evt);
            }
        },

        onCanvasPointerUp(evt) {
            if (this.dragMode) {
                this.endHandleDrag(evt);
                return;
            }
            const wasPinching = !!this._pinch;
            this._activePointers.delete(evt.pointerId);
            if (this._activePointers.size < 2) this._pinch = null;
            if (this._activePointers.size > 0) return; // fingers still down
            // All fingers up. A pinch is never a tap-to-deselect; only a clean
            // single-finger tap that didn't move deselects.
            if (!wasPinching && this._bgStart) {
                const moved = Math.hypot(evt.clientX - this._bgStart.x, evt.clientY - this._bgStart.y);
                if (moved < 3) this.selectedId = null;
            }
            this._bgStart = null;
            this.endPan();
        },

        // -- two-finger pinch-zoom + pan (touch) ------------------------------

        beginPinch() {
            const pts = [...this._activePointers.values()];
            if (pts.length < 2) return;
            const box = this.contentBox(); // client<->world geometry, fixed for the gesture
            const midX = (pts[0].x + pts[1].x) / 2;
            const midY = (pts[0].y + pts[1].y) / 2;
            this._pinch = {
                startDist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1,
                startW: this.viewBox.w,
                startH: this.viewBox.h,
                box: { left: box.left, top: box.top, width: box.width || 1, height: box.height || 1 },
                // World point under the finger midpoint at gesture start -- kept
                // pinned under the (possibly moving) midpoint for the whole
                // gesture, so pinch zooms toward the fingers and pans with them.
                anchorX: this.viewBox.x + (midX - box.left) * box.scaleX,
                anchorY: this.viewBox.y + (midY - box.top) * box.scaleY,
            };
        },

        onPinchMove() {
            const p = this._pinch;
            const pts = [...this._activePointers.values()];
            if (!p || pts.length < 2) return;
            const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
            const midX = (pts[0].x + pts[1].x) / 2;
            const midY = (pts[0].y + pts[1].y) / 2;
            const minW = this._fitW * 0.05;
            const maxW = this._fitW * 20;
            // Spread fingers (dist grows) -> narrower viewBox -> zoom in.
            const w = clamp((p.startW * p.startDist) / dist, minW, maxW);
            const h = w * (p.startH / p.startW);
            const scaleX = w / p.box.width;
            const scaleY = h / p.box.height;
            this.viewBox = {
                x: p.anchorX - (midX - p.box.left) * scaleX,
                y: p.anchorY - (midY - p.box.top) * scaleY,
                w,
                h,
            };
        },

        onWheelZoom(evt) {
            this.onWheel(evt);
        },

        fitAll() {
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
            for (const id of this.sectionOrder) {
                const section = this.sections[id];
                const seats = this.computeSeats(section).filter((s) => !s.removed);
                for (const seat of seats) {
                    minX = Math.min(minX, seat.x);
                    maxX = Math.max(maxX, seat.x);
                    minY = Math.min(minY, seat.y);
                    maxY = Math.max(maxY, seat.y);
                }
            }
            // Also make room for the SELECTED section's transform-box
            // handles -- the rotate handle in particular sits outside the
            // seat block by design (rotateHandleLocal()). Without this, Fit
            // can leave it beyond the fitted viewBox, where the <svg>'s
            // default clipping makes it invisible AND unclickable (confirmed
            // by driving the editor: bounding_box() still returned
            // coordinates for the clipped-out element, but clicking there
            // hit whatever page content sat behind/above the canvas).
            if (this.selected) {
                // Round 3 #7 / Round 4: none of the transform-box handles
                // are hidden for arc sections any more -- offset now
                // composes with arc too (round-4 correction) -- so every
                // handle unconditionally needs room in Fit.
                const points = [
                    this.handleOrigin(), this.handleRotate(), this.originMarkerXY(),
                    this.pivotWorldXY(), this.rotationPivotMarkerXY(), this.handleOffset(),
                    this.handleTL(), this.handleTR(), this.handleBL(), this.handleBR(),
                ];
                for (const [x, y] of points) {
                    minX = Math.min(minX, x);
                    maxX = Math.max(maxX, x);
                    minY = Math.min(minY, y);
                    maxY = Math.max(maxY, y);
                }
            }
            if (!isFinite(minX)) {
                this.fitTo(-1, -1, 10, 10);
                return;
            }
            this.fitTo(minX, minY, maxX - minX, maxY - minY);
        },

        // -- seat popover -----------------------------------------------------

        openPopover(sectionId, seat, evt) {
            this.popover = {
                sectionId,
                row: seat.row,
                number: seat.number,
                accessible: seat.accessible,
                screenX: evt.clientX,
                screenY: evt.clientY,
            };
        },

        closePopover() {
            this.popover = null;
        },

        popoverToggleAda() {
            if (!this.popover) return;
            this.beginCoalesce();
            const s = this.sections[this.popover.sectionId];
            const key = seatKey(this.popover.row, this.popover.number);
            if (s.accessibleIds.has(key)) {
                s.accessibleIds.delete(key);
            } else {
                s.accessibleIds.add(key);
            }
            this.popover.accessible = s.accessibleIds.has(key);
            this.dirty = true;
            this.renderSection(this.popover.sectionId);
            this.endCoalesce();
        },

        popoverDeleteSeat() {
            if (!this.popover) return;
            this.beginCoalesce();
            const s = this.sections[this.popover.sectionId];
            const key = seatKey(this.popover.row, this.popover.number);
            s.removedIds.add(key);
            s.accessibleIds.delete(key);
            this.dirty = true;
            this.renderSection(this.popover.sectionId);
            this.endCoalesce();
            this.popover = null;
        },

        // -- inline "New section" (Round 2, docs/EDITOR.md #7) -----------------
        //
        // Posts to the SAME dashboard_section_create endpoint a direct link
        // to /sections/new/ would (manager-gated, org-/chart-scoped --
        // SectionCreateView's docstring), but with an X-Requested-With
        // header so the view returns JSON instead of redirecting. The
        // response's `section` is in the exact shape makeSection() expects
        // (dashboard.views._section_json), so the new section becomes
        // indistinguishable from one the page loaded with -- it gets a
        // group (ensureSectionGroup), renders, and is selected, all without
        // leaving the editor or reloading anything.

        csrfToken() {
            const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
            return match ? decodeURIComponent(match[1]) : "";
        },

        openNewSection() {
            this.newSectionForm = { name: "", tier: "" };
            this.newSectionError = null;
            this.newSectionOpen = true;
        },

        closeNewSection() {
            this.newSectionOpen = false;
        },

        async submitNewSection() {
            if (this.newSectionSaving) return;
            const name = (this.newSectionForm.name || "").trim();
            if (!name) {
                this.newSectionError = "Name is required.";
                return;
            }
            this.newSectionSaving = true;
            this.newSectionError = null;
            try {
                const resp = await fetch(this.newSectionUrl, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-CSRFToken": this.csrfToken(),
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    credentials: "same-origin",
                    body: new URLSearchParams({
                        name,
                        tier: this.newSectionForm.tier || "",
                        numbering_scheme: "sequential",
                        row_label_scheme: "skip_io",
                    }),
                });
                let data = null;
                try {
                    data = await resp.json();
                } catch (e) {
                    // Non-JSON error page (e.g. a 403) -- fall through to
                    // the generic error message below.
                }
                if (!resp.ok || !data || !data.ok) {
                    const fieldErrors = data && data.errors;
                    this.newSectionError =
                        (fieldErrors && Object.values(fieldErrors)[0] && Object.values(fieldErrors)[0][0]) ||
                        "Could not create the section. Try again.";
                    return;
                }
                const section = makeSection(data.section);
                this.sections[section.id] = section;
                this.sectionOrder.push(section.id);
                // Inline create makes a real Section row server-side (see this
                // method's doc comment) -- it isn't undoable, so wipe history
                // rather than let an undo restore a snapshot missing this
                // section and orphan the DB row. See clearHistory().
                this.clearHistory();
                this.newSectionOpen = false;
                this.$nextTick(() => {
                    this.renderSection(section.id);
                    this.selectSection(section.id);
                    this.fitAll();
                });
            } catch (e) {
                this.newSectionError = "Network error -- try again.";
            } finally {
                this.newSectionSaving = false;
            }
        },

        buildPayload() {
            const sections = {};
            for (const id of this.sectionOrder) {
                const s = this.sections[id];
                const params = {};
                for (const field of PARAM_FIELDS) params[field] = s[field];
                // Round 4: s.arc_radius is always current now (0 for
                // straight, no more enabled-flag indirection) -- 0 maps to
                // null the same way the server's "off" sentinel does (see
                // dashboard/views.py's chart_editor_save arc_radius branch).
                params.arc_radius = s.arc_radius || null;
                params.removed = [...s.removedIds].map((k) => k.split("|"));
                params.accessible = [...s.accessibleIds].map((k) => k.split("|"));
                sections[id] = params;
            }
            return { sections };
        },

        async save() {
            if (this.saving) return;
            this.saving = true;
            this.error = null;
            try {
                const resp = await fetch(this.saveUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": this.csrfToken() },
                    credentials: "same-origin",
                    body: JSON.stringify(this.buildPayload()),
                });
                let data = null;
                try {
                    data = await resp.json();
                } catch (e) {
                    // Non-JSON error page (e.g. a 403/500) -- fall through.
                }
                if (!resp.ok || !data) {
                    this.error = `Save failed (${resp.status}). Try again.`;
                    return;
                }
                if (data.errors && Object.keys(data.errors).length) {
                    const messages = Object.values(data.errors);
                    this.error = messages[0] + (messages.length > 1 ? ` (+${messages.length - 1} more)` : "");
                }
                if (!data.ok && !(data.errors && Object.keys(data.errors).length)) {
                    this.error = data.error || "Save failed. Try again.";
                    return;
                }
                if (!data.errors || Object.keys(data.errors).length === 0) {
                    this.dirty = false;
                }
                this.savedAt = new Date();
            } catch (e) {
                this.error = "Network error -- try again.";
            } finally {
                this.saving = false;
            }
        },
    };
}
