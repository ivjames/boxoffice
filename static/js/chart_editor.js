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
        arc_enabled: !!arcRadius,
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
        _bgStart: null,
        popover: null, // {sectionId, row, number, accessible, screenX, screenY}
        dirty: false,
        saving: false,
        savedAt: null,
        error: null,
        newSectionOpen: false,
        newSectionForm: { name: "", tier: "" },
        newSectionSaving: false,
        newSectionError: null,

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
            this.$watch("viewBox", () => this.syncViewBoxAttr());
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
            });
            window.addEventListener("beforeunload", (evt) => {
                if (this.dirty) {
                    evt.preventDefault();
                    evt.returnValue = "";
                }
            });
        },

        get selected() {
            return this.selectedId != null ? this.sections[this.selectedId] : null;
        },

        get selectedSeatCount() {
            const s = this.selected;
            return s ? s.seatCount : 0;
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
                arc_radius: section.arc_enabled ? section.arc_radius : 0,
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
                circle.addEventListener("pointerdown", (evt) => {
                    evt.stopPropagation();
                    evt.preventDefault();
                    this.selectSection(id);
                    this.openPopover(id, seat, evt);
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
            }
        },

        onParamInput(id) {
            this.dirty = true;
            this.renderSection(id);
        },

        onArcToggle(id) {
            const s = this.sections[id];
            s.arc_enabled = !s.arc_enabled;
            if (s.arc_enabled && !s.arc_amount) s.arc_amount = 20;
            s.arc_radius = arcAmountToRadius(s.arc_enabled ? s.arc_amount : 0);
            this.onParamInput(id);
        },

        onArcAmountInput(id) {
            const s = this.sections[id];
            s.arc_radius = arcAmountToRadius(s.arc_amount);
            s.arc_enabled = s.arc_radius > 0;
            this.onParamInput(id);
        },

        onOffsetModeInput(id, mode) {
            this.sections[id].offset_mode = mode;
            this.onParamInput(id);
        },

        // Round 2 (docs/EDITOR.md #2): the Center/Origin/Custom selector.
        // Switching INTO "custom" seeds pivot_x/pivot_y from whatever the
        // pivot is currently computed as (center or origin) so the pivot
        // marker doesn't jump somewhere unexpected the moment the mode
        // changes -- from there, dragging the marker (onHandleDrag's
        // 'move_pivot' case) moves it exactly where the user drops it.
        onPivotModeInput(id, mode) {
            const s = this.sections[id];
            if (mode === "custom" && s.pivot_mode !== "custom") {
                const [px, py] = window.SeatGeometry.pivotLocal(this.geomParams(s));
                s.pivot_x = px;
                s.pivot_y = py;
            }
            s.pivot_mode = mode;
            this.onParamInput(id);
        },

        stepRows(id, delta) {
            const s = this.sections[id];
            s.rows = Math.max(1, s.rows + delta);
            this.onParamInput(id);
        },

        stepSeatsPerRow(id, delta) {
            const s = this.sections[id];
            s.seats_per_row = Math.max(1, s.seats_per_row + delta);
            this.onParamInput(id);
        },

        stepAltDelta(id, delta) {
            const s = this.sections[id];
            s.alt_row_seat_delta = s.alt_row_seat_delta + delta;
            this.onParamInput(id);
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

        // -- transform box: local <-> world, handle positions -----------------

        localWH(section) {
            const w = Math.max(0, section.seats_per_row - 1) * section.seat_pitch;
            const h = Math.max(0, section.rows - 1) * section.row_pitch;
            return [w, h];
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

        // The origin ("move section") MARKER is drawn/clickable slightly
        // up-and-left of the TRUE origin (handleOrigin()), not exactly on
        // top of it. The true origin usually coincides with the section's
        // own front-left seat, and since the marker paints after (on top
        // of) the seat circles, a marker drawn exactly at the origin would
        // sit on top of that seat and swallow every click meant for it --
        // confirmed by driving the editor: clicking that seat opened a drag
        // instead of its popover. A thin connector line ties the marker
        // back to the real origin point so it's unambiguous. This offset is
        // a plain constant delta (NOT routed through worldFromLocal/
        // rotation) since the origin itself doesn't rotate.
        originMarkerXY() {
            const s = this.selected;
            if (!s) return [0, 0];
            const dx = -Math.max(0.5, s.seat_pitch * 0.6);
            const dy = -Math.max(0.5, s.row_pitch * 0.6);
            return [s.origin_x + dx, s.origin_y + dy];
        },

        // The ROTATION PIVOT marker (Round 2, docs/EDITOR.md #2): offset
        // from the true pivot (pivotWorldXY()) in the OPPOSITE direction of
        // the origin marker above, so the two draggable markers/connectors
        // never sit on top of each other -- including the common case where
        // pivot_mode is ORIGIN (pivot === origin exactly) or a small block's
        // CENTER pivot lands close to it. Dragging this marker always sets
        // pivot_mode to CUSTOM (see onHandleDrag's 'move_pivot' case) --
        // it's the one control that works regardless of which mode is
        // currently selected in the Center/Origin/Custom toggle.
        rotationPivotMarkerXY() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [px, py] = this.pivotWorldXY();
            const dx = Math.max(0.5, s.seat_pitch * 0.6);
            const dy = Math.max(0.5, s.row_pitch * 0.6);
            return [px + dx, py + dy];
        },

        handleTR() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [w] = this.localWH(s);
            return this.worldFromLocal(s, w, 0);
        },

        handleBL() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [, h] = this.localWH(s);
            return this.worldFromLocal(s, 0, h);
        },

        handleBR() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [w, h] = this.localWH(s);
            return this.worldFromLocal(s, w, h);
        },

        rotateHandleLocal(s) {
            const [w] = this.localWH(s);
            const dist = Math.max(1.5, w * 0.15 + 1.5);
            return [w / 2, -dist];
        },

        handleRotate() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [lx, ly] = this.rotateHandleLocal(s);
            return this.worldFromLocal(s, lx, ly);
        },

        handleOffset() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [, h] = this.localWH(s);
            if (s.offset_mode === "alternating") {
                return this.worldFromLocal(s, s.row_x_offset, Math.min(h, s.row_pitch));
            }
            return this.worldFromLocal(s, Math.max(0, s.rows - 1) * s.row_x_offset, h);
        },

        // -- transform box: dragging --------------------------------------

        startHandleDrag(mode, evt) {
            if (!this.selected) return;
            evt.preventDefault();
            evt.stopPropagation();
            this.dragMode = mode;
            this._dragSectionId = this.selectedId;
            this._dragStart = { pointer: this.clientToViewBox(evt.clientX, evt.clientY) };
            if (evt.target.setPointerCapture) evt.target.setPointerCapture(evt.pointerId);
        },

        onCanvasPointerMove(evt) {
            if (this.dragMode) {
                this.onHandleDrag(evt);
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
                const [startPx, startPy] = this._dragStart.pointer;
                s.origin_x += px - startPx;
                s.origin_y += py - startPy;
                this._dragStart.pointer = [px, py];
            } else if (this.dragMode === "move_pivot") {
                // Reposition the ROTATION PIVOT (Round 2, docs/EDITOR.md
                // #2): a pivot's world position is always origin +
                // pivot_local with NO rotation applied (see
                // seat_geometry.js's pivotXY doc comment), so converting the
                // dropped world point back to local is just subtraction --
                // no toLocal()/rotation-inversion needed. Always switches
                // pivot_mode to CUSTOM, regardless of what it was before.
                s.pivot_mode = "custom";
                s.pivot_x = px - s.origin_x;
                s.pivot_y = py - s.origin_y;
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
                const [lx] = this.toLocal(s, px, py);
                if (s.offset_mode === "alternating") {
                    s.row_x_offset = clamp(lx, -30, 30);
                } else {
                    const denom = Math.max(1, s.rows - 1);
                    s.row_x_offset = clamp(lx / denom, -30, 30);
                }
            }
            this.dirty = true;
            this.renderSection(this._dragSectionId);
        },

        endHandleDrag() {
            this.dragMode = null;
            this._dragSectionId = null;
            this._dragStart = null;
        },

        // -- background: pan + click-to-deselect ------------------------------

        onCanvasPointerDown(evt) {
            this.popover = null;
            this._bgStart = { x: evt.clientX, y: evt.clientY };
            this.startPan(evt);
        },

        onCanvasPointerUp(evt) {
            if (this.dragMode) {
                this.endHandleDrag();
                return;
            }
            if (this._bgStart) {
                const moved = Math.hypot(evt.clientX - this._bgStart.x, evt.clientY - this._bgStart.y);
                if (moved < 3) this.selectedId = null;
            }
            this._bgStart = null;
            this.endPan();
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
                const points = [
                    this.handleOrigin(), this.handleRotate(), this.originMarkerXY(),
                    this.pivotWorldXY(), this.rotationPivotMarkerXY(),
                ];
                if (!this.selected.arc_enabled) {
                    points.push(this.handleTR(), this.handleBL(), this.handleBR(), this.handleOffset());
                }
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
        },

        popoverDeleteSeat() {
            if (!this.popover) return;
            const s = this.sections[this.popover.sectionId];
            const key = seatKey(this.popover.row, this.popover.number);
            s.removedIds.add(key);
            s.accessibleIds.delete(key);
            this.dirty = true;
            this.renderSection(this.popover.sectionId);
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
                params.arc_radius = s.arc_enabled ? s.arc_radius : null;
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
