/*
 * Phase B seating-chart visual editor (dashboard/templates/dashboard/
 * chart_editor.html) -- Alpine component backing the inline-SVG drag
 * editor. Per the epic's locked decision (docs/SEATING.md "Decisions
 * (locked)": "in-house SVG drag, no canvas library"), every seat is a real
 * server-rendered <circle> (Django template loop, NOT Alpine x-for --
 * see the big comment in templates/orders/_seat_map.html about x-for
 * breaking inside <svg>; this component sidesteps that entirely by never
 * cloning DOM inside the <svg>, only reactively binding cx/cy on elements
 * that already exist). Alpine happily binds directives to existing SVG
 * elements (it feature-detects SVGElement and uses setAttribute instead of
 * property assignment), so :cx/:cy/@pointerdown work fine here.
 *
 * Drag mechanics: pointerdown on a seat starts a drag, recording the
 * client-space -> viewBox-space scale factor from the <svg>'s current
 * bounding box (so dragging feels 1:1 regardless of how the SVG is scaled
 * on screen); pointermove/pointerup are bound at the window level (guarded
 * by `dragging`) so a fast drag that leaves the seat's hit area doesn't
 * drop the gesture. Every dragged seat's id goes into `dirty`; "Save
 * changes" POSTs {positions: {seat_id: {x, y}, ...}} as JSON to the
 * chart's save endpoint (dashboard.views.chart_editor_save) with the
 * standard Django CSRF cookie->header handoff (CSRF_COOKIE_HTTPONLY is not
 * set in this project -- see config/settings -- so document.cookie can
 * read it, same assumption the rest of the app's forms rely on implicitly
 * via the auto-included csrfmiddlewaretoken input).
 *
 * Selection hooks for Phase C (drag-select pricing zones, docs/SEATING.md
 * "C"): every seat <circle> carries data-seat-id/data-section-id and the
 * `.editor-seat` class; this component's `seatsById` map is the single
 * source of truth for each seat's current (possibly-dragged) position.
 * Phase C's marquee/shift-click selection can read seat screen positions
 * via getBoundingClientRect() on `.editor-seat` elements (same technique
 * `startDrag` below uses for the viewBox scale) and reuse this file's
 * pointer-capture pattern instead of reinventing it.
 */

function chartEditor(saveUrl) {
    return {
        seatsById: {},
        dragging: null, // { id, startClientX, startClientY, startX, startY, scaleX, scaleY }
        dirty: new Set(),
        saving: false,
        savedAt: null,
        error: null,

        init() {
            const seats = JSON.parse(document.getElementById("editor-seats-data").textContent);
            const byId = {};
            for (const seat of seats) {
                byId[seat.id] = { x: seat.x, y: seat.y };
            }
            this.seatsById = byId;

            window.addEventListener("beforeunload", (evt) => {
                if (this.dirty.size > 0) {
                    evt.preventDefault();
                    evt.returnValue = "";
                }
            });
        },

        get dirtyCount() {
            return this.dirty.size;
        },

        startDrag(seatId, evt) {
            evt.preventDefault();
            const svg = this.$refs.svg;
            const rect = svg.getBoundingClientRect();
            const viewBox = svg.viewBox.baseVal;
            const seat = this.seatsById[seatId];
            if (!seat || !rect.width || !rect.height) return;
            this.dragging = {
                id: seatId,
                startClientX: evt.clientX,
                startClientY: evt.clientY,
                startX: seat.x,
                startY: seat.y,
                scaleX: viewBox.width / rect.width,
                scaleY: viewBox.height / rect.height,
            };
            if (evt.target.setPointerCapture) {
                evt.target.setPointerCapture(evt.pointerId);
            }
        },

        onDrag(evt) {
            if (!this.dragging) return;
            evt.preventDefault();
            const d = this.dragging;
            const dx = (evt.clientX - d.startClientX) * d.scaleX;
            const dy = (evt.clientY - d.startClientY) * d.scaleY;
            this.seatsById[d.id].x = d.startX + dx;
            this.seatsById[d.id].y = d.startY + dy;
            this.dirty.add(d.id);
        },

        endDrag() {
            this.dragging = null;
        },

        seatClass(seatId) {
            return this.dragging && this.dragging.id === seatId ? "editor-seat--dragging" : "";
        },

        csrfToken() {
            const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
            return match ? decodeURIComponent(match[1]) : "";
        },

        async save() {
            if (this.dirty.size === 0 || this.saving) return;
            this.saving = true;
            this.error = null;
            const positions = {};
            for (const id of this.dirty) {
                positions[id] = { x: this.seatsById[id].x, y: this.seatsById[id].y };
            }
            try {
                const resp = await fetch(saveUrl, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": this.csrfToken(),
                    },
                    credentials: "same-origin",
                    body: JSON.stringify({ positions }),
                });
                let data = null;
                try {
                    data = await resp.json();
                } catch (e) {
                    // Non-JSON error page (e.g. a 403/500) -- fall through
                    // to the generic status-based message below.
                }
                if (!resp.ok || !data || !data.ok) {
                    this.error = (data && data.error) || `Save failed (${resp.status}). Try again.`;
                    return;
                }
                this.dirty = new Set();
                this.savedAt = new Date();
            } catch (e) {
                this.error = "Network error -- try again.";
            } finally {
                this.saving = false;
            }
        },
    };
}
