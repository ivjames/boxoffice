/*
 * Phase C seating-chart visual pricing-zone editor (dashboard/templates/
 * dashboard/zone_editor.html) -- Alpine component for marquee (rubber-band)
 * + shift-click seat selection, then applying/removing/deleting zones and
 * cloning zones from another performance. Per docs/SEATING.md's locked
 * decisions and the Phase B hand-off comment in chart_editor.js: reuses the
 * same server-rendered `<circle class="editor-seat" data-seat-id
 * data-section-id>` SVG seat elements (Django template loop, not Alpine
 * x-for -- see templates/orders/_seat_map.html's big comment on why x-for
 * breaks inside <svg>), and reads seat screen positions via
 * getBoundingClientRect() for marquee hit-testing instead of doing viewBox
 * math, so it works correctly no matter how the SVG is scaled on screen.
 *
 * Selection model: `selected` is a Set of seat ids (Alpine's reactivity,
 * built on @vue/reactivity, tracks Set add/delete/has() -- chart_editor.js's
 * `dirty` Set already relies on the same thing for its unsaved-seat count).
 * A plain click on a seat REPLACES the selection with just that seat; a
 * shift-click TOGGLES it into/out of the current selection; a drag that
 * starts on empty SVG background draws a marquee rectangle in screen
 * (client) coordinates and, on release, selects every seat whose center
 * falls inside it -- replacing the selection, or unioning with it if shift
 * was held when the drag started.
 *
 * Zone data (`zones`) is the exact shape dashboard.views._zones_payload
 * returns -- [{id, name, color, amount, template_id, seat_ids}] -- and
 * every mutation endpoint (apply/remove/delete/clone) returns that same
 * shape back, so a successful call just replaces `this.zones` wholesale
 * instead of re-fetching the page.
 */

function zoneEditor(config) {
    return {
        zones: [],
        templatesJson: [],
        selected: new Set(),
        marquee: null, // {startX, startY, curX, curY, additive} in CLIENT (screen) coords
        saving: false,
        error: null,
        notice: null,

        applyMode: "template",
        applyTemplateId: "",
        applyName: "",
        applyColor: "#2563eb",
        applyAmount: "",
        cloneSourceId: "",

        applyUrl: config.applyUrl,
        removeUrl: config.removeUrl,
        deleteUrlPrefix: config.deleteUrlPrefix,
        cloneUrl: config.cloneUrl,

        init() {
            this.zones = JSON.parse(document.getElementById("zone-editor-zones-data").textContent);
            this.templatesJson = JSON.parse(
                document.getElementById("zone-editor-templates-data").textContent
            );
        },

        // -- zone lookups ----------------------------------------------------

        zoneForSeat(seatId) {
            for (const zone of this.zones) {
                if (zone.seat_ids.includes(seatId)) return zone;
            }
            return null;
        },

        seatFill(seatId) {
            const zone = this.zoneForSeat(seatId);
            return zone ? zone.color : "#d1d5db";
        },

        seatClass(seatId) {
            return this.selected.has(seatId) ? "editor-seat--selected" : "";
        },

        get selectedCount() {
            return this.selected.size;
        },

        clearSelection() {
            this.selected = new Set();
        },

        // -- selection: click / shift-click -----------------------------------

        onSeatPointerDown(seatId, evt) {
            if (evt.shiftKey) {
                if (this.selected.has(seatId)) {
                    this.selected.delete(seatId);
                } else {
                    this.selected.add(seatId);
                }
            } else {
                this.selected = new Set([seatId]);
            }
        },

        // -- selection: marquee drag ------------------------------------------

        onBackgroundPointerDown(evt) {
            // Seat circles call @pointerdown.stop, so this only fires when the
            // drag starts on empty SVG background.
            evt.preventDefault();
            this.marquee = {
                startX: evt.clientX,
                startY: evt.clientY,
                curX: evt.clientX,
                curY: evt.clientY,
                additive: evt.shiftKey,
            };
        },

        onDrag(evt) {
            if (!this.marquee) return;
            evt.preventDefault();
            this.marquee.curX = evt.clientX;
            this.marquee.curY = evt.clientY;
        },

        endDrag() {
            if (!this.marquee) return;
            const rect = this.marqueeRect;
            const additive = this.marquee.additive;
            const isClick = rect.width < 3 && rect.height < 3;
            this.marquee = null;
            if (isClick) {
                // A bare click (no real drag) on empty background: plain click
                // clears the selection (matches clicking empty space elsewhere
                // in the app); shift-click on empty background is a no-op so it
                // never accidentally wipes an in-progress shift-click selection.
                if (!additive) this.clearSelection();
                return;
            }

            const hits = [];
            document.querySelectorAll(".zone-editor-seat").forEach((el) => {
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                if (cx >= rect.left && cx <= rect.right && cy >= rect.top && cy <= rect.bottom) {
                    hits.push(parseInt(el.dataset.seatId, 10));
                }
            });
            if (additive) {
                for (const id of hits) this.selected.add(id);
            } else {
                this.selected = new Set(hits);
            }
        },

        get marqueeRect() {
            if (!this.marquee) return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
            const left = Math.min(this.marquee.startX, this.marquee.curX);
            const right = Math.max(this.marquee.startX, this.marquee.curX);
            const top = Math.min(this.marquee.startY, this.marquee.curY);
            const bottom = Math.max(this.marquee.startY, this.marquee.curY);
            return { left, right, top, bottom, width: right - left, height: bottom - top };
        },

        get marqueeStyle() {
            if (!this.marquee) return "display:none;";
            const r = this.marqueeRect;
            return (
                `display:block; position:fixed; left:${r.left}px; top:${r.top}px; ` +
                `width:${r.width}px; height:${r.height}px;`
            );
        },

        // -- server calls ------------------------------------------------------

        csrfToken() {
            const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
            return match ? decodeURIComponent(match[1]) : "";
        },

        async postJson(url, body) {
            this.error = null;
            this.notice = null;
            try {
                const resp = await fetch(url, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": this.csrfToken(),
                    },
                    credentials: "same-origin",
                    body: JSON.stringify(body),
                });
                let data = null;
                try {
                    data = await resp.json();
                } catch (e) {
                    // Non-JSON error page (e.g. a 403/500) -- fall through.
                }
                if (!resp.ok || !data || !data.ok) {
                    this.error = (data && data.error) || `Request failed (${resp.status}). Try again.`;
                    return null;
                }
                return data;
            } catch (e) {
                this.error = "Network error -- try again.";
                return null;
            }
        },

        async applyZone() {
            if (this.selected.size === 0) {
                this.error = "Select some seats first.";
                return;
            }
            if (!this.applyAmount) {
                this.error = "Enter a price.";
                return;
            }
            const body = { seat_ids: [...this.selected], amount: this.applyAmount };
            if (this.applyMode === "template") {
                if (!this.applyTemplateId) {
                    this.error = "Pick a zone template.";
                    return;
                }
                body.template_id = parseInt(this.applyTemplateId, 10);
            } else {
                if (!this.applyName || !this.applyColor) {
                    this.error = "Give the new zone a name and color.";
                    return;
                }
                body.name = this.applyName;
                body.color = this.applyColor;
            }
            this.saving = true;
            const data = await this.postJson(this.applyUrl, body);
            this.saving = false;
            if (data) {
                this.zones = data.zones;
                this.notice = "Zone applied.";
                this.clearSelection();
            }
        },

        async removeSelectedFromZone(zoneId) {
            if (this.selected.size === 0) return;
            this.saving = true;
            const data = await this.postJson(this.removeUrl, {
                zone_id: zoneId,
                seat_ids: [...this.selected],
            });
            this.saving = false;
            if (data) {
                this.zones = data.zones;
                this.notice = "Removed from zone.";
            }
        },

        async deleteZone(zoneId) {
            if (
                !confirm(
                    "Delete this zone? Its seats fall back to the section price. Any hold/order " +
                        "that already snapshotted this zone's price keeps that price."
                )
            ) {
                return;
            }
            this.saving = true;
            const data = await this.postJson(this.deleteUrlPrefix + zoneId + "/delete/", {});
            this.saving = false;
            if (data) {
                this.zones = data.zones;
                this.notice = "Zone deleted.";
            }
        },

        async cloneZones() {
            if (!this.cloneSourceId) {
                this.error = "Pick a performance to clone from.";
                return;
            }
            this.saving = true;
            const data = await this.postJson(this.cloneUrl, {
                source_performance_id: parseInt(this.cloneSourceId, 10),
            });
            this.saving = false;
            if (data) {
                this.zones = data.zones;
                this.notice = "Zones cloned.";
            }
        },
    };
}
