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
 */

const SVG_NS = "http://www.w3.org/2000/svg";

const PARAM_FIELDS = [
    "origin_x", "origin_y", "rotation", "seat_pitch", "row_pitch", "row_x_offset",
    "offset_mode", "alt_row_seat_delta", "rows", "seats_per_row",
    "numbering_scheme", "row_label_scheme",
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
        origin_x: raw.origin_x,
        origin_y: raw.origin_y,
        rotation: raw.rotation,
        seat_pitch: raw.seat_pitch,
        row_pitch: raw.row_pitch,
        row_x_offset: raw.row_x_offset,
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
        sections: {},
        sectionOrder: [],
        selectedId: config.initialSelectedId || null,
        dragMode: null, // null | 'pivot' | 'seat_pitch' | 'row_pitch' | 'both' | 'rotate' | 'offset'
        _dragSectionId: null,
        _dragStart: null,
        _bgStart: null,
        popover: null, // {sectionId, row, number, accessible, screenX, screenY}
        dirty: false,
        saving: false,
        savedAt: null,
        error: null,

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
            this.$nextTick(() => {
                for (const id of this.sectionOrder) this.renderSection(id);
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
            };
        },

        computeSeats(section) {
            return window.SeatGeometry.computeSectionSeats(this.geomParams(section), {
                removedIds: section.removedIds,
                accessibleIds: section.accessibleIds,
            });
        },

        renderSection(id) {
            const section = this.sections[id];
            if (!section) return;
            const g = this.$refs.svg.querySelector(`[data-section-group="${id}"]`);
            if (!g) return;
            while (g.firstChild) g.removeChild(g.firstChild);

            const seats = this.computeSeats(section).filter((s) => !s.removed);
            section.seatCount = seats.length;
            const radius = Math.max(0.15, section.seat_pitch * 0.35);

            for (const seat of seats) {
                const circle = document.createElementNS(SVG_NS, "circle");
                circle.setAttribute("cx", seat.x);
                circle.setAttribute("cy", seat.y);
                circle.setAttribute("r", radius);
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

        // -- transform box: local <-> world, handle positions -----------------

        localWH(section) {
            const w = Math.max(0, section.seats_per_row - 1) * section.seat_pitch;
            const h = Math.max(0, section.rows - 1) * section.row_pitch;
            return [w, h];
        },

        worldFromLocal(section, lx, ly) {
            const [rx, ry] = window.SeatGeometry.rotate(lx, ly, section.rotation);
            return [section.origin_x + rx, section.origin_y + ry];
        },

        toLocal(section, worldX, worldY) {
            const dx = worldX - section.origin_x;
            const dy = worldY - section.origin_y;
            return window.SeatGeometry.rotate(dx, dy, -section.rotation);
        },

        handlePivot() {
            const s = this.selected;
            return s ? [s.origin_x, s.origin_y] : [0, 0];
        },

        // The pivot MARKER is drawn/clickable slightly up-and-left of the
        // TRUE pivot (handlePivot(), the section's origin -- what rotation
        // actually pivots on), not exactly on top of it. The true origin
        // usually coincides with the section's own front-left seat, and
        // since the marker paints after (on top of) the seat circles, a
        // marker drawn exactly at the origin would sit on top of that seat
        // and swallow every click meant for it -- confirmed by driving the
        // editor: clicking that seat opened a pivot drag instead of its
        // popover. Dragging the marker still moves origin_x/origin_y
        // exactly as if it were at the true origin (onHandleDrag's 'pivot'
        // case only cares about pointer delta, not the marker's own
        // position); a thin connector line ties the marker back to the
        // real pivot point so it's unambiguous.
        pivotMarkerLocal(s) {
            return [-Math.max(0.5, s.seat_pitch * 0.6), -Math.max(0.5, s.row_pitch * 0.6)];
        },

        pivotMarkerXY() {
            const s = this.selected;
            if (!s) return [0, 0];
            const [lx, ly] = this.pivotMarkerLocal(s);
            return this.worldFromLocal(s, lx, ly);
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

            if (this.dragMode === "pivot") {
                const [startPx, startPy] = this._dragStart.pointer;
                s.origin_x += px - startPx;
                s.origin_y += py - startPy;
                this._dragStart.pointer = [px, py];
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
                const originVec = [px - s.origin_x, py - s.origin_y];
                const pointerAngle = Math.atan2(originVec[1], originVec[0]);
                const [hx, hy] = this.rotateHandleLocal(s);
                const handleAngle = Math.atan2(hy, hx);
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
                const points = [this.handlePivot(), this.handleRotate(), this.pivotMarkerXY()];
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

        // -- save ---------------------------------------------------------------

        csrfToken() {
            const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
            return match ? decodeURIComponent(match[1]) : "";
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
