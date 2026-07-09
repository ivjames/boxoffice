/*
 * Shared viewport controller for the dashboard's inline-SVG editors
 * (chart_editor.js and zone_editor.js, per docs/EDITOR.md's "put the
 * shared viewport ... in one small JS module included by BOTH editors").
 * Provides wheel-zoom-to-cursor, pan-on-empty-background, and a Fit action,
 * all built on one correctly-computed client-px -> viewBox-unit scale.
 *
 * THE BUG THIS FIXES: the old per-editor drag code (see chart_editor.js's
 * git history) computed that scale as `viewBox.width / rect.width` /
 * `viewBox.height / rect.height`, where `rect` is the <svg> element's own
 * getBoundingClientRect(). That's only correct if the element's aspect
 * ratio exactly matches the viewBox's aspect ratio. With
 * preserveAspectRatio="xMidYMid meet" (what both editors use), a mismatch
 * means the rendered content is letterboxed -- padded on one axis -- so
 * `rect` is BIGGER on that axis than the actual content, and dividing by
 * the full rect height/width understates the true scale on exactly that
 * axis. In practice the editors' SVGs are usually wider (landscape
 * viewBox) than their container's aspect, so height gets letterboxed and
 * the Y scale comes out too small -- a drag/handle that should track the
 * cursor 1:1 only moves at a fraction of cursor speed vertically. `contentBox()`
 * below computes the TRUE rendered content rectangle (accounting for the
 * meet letterboxing) and derives scaleX/scaleY from THAT, so both axes
 * are correct regardless of aspect mismatch.
 *
 * Usage: `Object.assign(alpineData, EditorViewport.mixin())` inside a
 * component factory function (see chart_editor.js/zone_editor.js), which
 * merges in `viewBox` state + the methods below. Deliberately built from
 * plain data + plain methods only (no getters) so a plain Object.assign
 * copy is safe -- getters would be evaluated-and-flattened by the copy,
 * losing their reactivity.
 */

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

function mixin() {
    return {
        // Current viewBox, in the SVG's own coordinate units.
        viewBox: { x: 0, y: 0, w: 10, h: 10 },
        _fitW: 10,
        _fitH: 10,
        _panState: null,

        viewBoxAttr() {
            const vb = this.viewBox;
            return `${vb.x} ${vb.y} ${vb.w} ${vb.h}`;
        },

        /*
         * Push `viewBox` onto the <svg>'s real `viewBox` DOM attribute.
         * MUST be called imperatively (this.$refs.svg.setAttribute(...))
         * rather than via an Alpine `:viewBox="..."` bind: the HTML parser
         * lowercases ALL attribute names before Alpine ever sees them
         * (confirmed: `:viewBox="x"` in markup becomes the DOM attribute
         * `:viewbox`, not `:viewBox`), and SVG's `viewBox` attribute is
         * case-SENSITIVE via setAttribute -- so an Alpine bind silently
         * sets a dead `viewbox` (lowercase) attribute that does nothing,
         * and the SVG never re-frames. Call this from a `$watch("viewBox",
         * ...)` set up in the component's init() instead (see chart_editor.js
         * / zone_editor.js).
         */
        syncViewBoxAttr() {
            const svg = this.$refs.svg;
            if (svg) svg.setAttribute("viewBox", this.viewBoxAttr());
        },

        // The true rendered content box (accounting for preserveAspectRatio
        // "meet" letterboxing) in CLIENT (screen) pixels, plus the
        // corresponding per-axis client-px -> viewBox-unit scale. This is
        // the single source of truth every pointer-math method below uses
        // -- see the module comment for why naive rect.width/height is wrong.
        contentBox() {
            const svg = this.$refs.svg;
            const rect = svg.getBoundingClientRect();
            const vb = this.viewBox;
            if (!rect.width || !rect.height || !vb.w || !vb.h) {
                return { left: rect.left, top: rect.top, width: rect.width || 1, height: rect.height || 1, scaleX: 1, scaleY: 1 };
            }
            const vbAspect = vb.w / vb.h;
            const rectAspect = rect.width / rect.height;
            let width, height, offsetX = 0, offsetY = 0;
            if (rectAspect > vbAspect) {
                // Rect is relatively wider than the viewBox -- full height,
                // letterboxed left/right.
                height = rect.height;
                width = height * vbAspect;
                offsetX = (rect.width - width) / 2;
            } else {
                // Rect is relatively taller -- full width, letterboxed
                // top/bottom (the case that produced the vertical 0.5x bug
                // when un-corrected).
                width = rect.width;
                height = width / vbAspect;
                offsetY = (rect.height - height) / 2;
            }
            return {
                left: rect.left + offsetX,
                top: rect.top + offsetY,
                width,
                height,
                scaleX: vb.w / width,
                scaleY: vb.h / height,
            };
        },

        // Client (screen) coordinates -> viewBox coordinates, per-axis
        // correct. Every editor pointer handler (drag, transform handles,
        // pan) should go through this instead of rolling its own scale.
        clientToViewBox(clientX, clientY) {
            const box = this.contentBox();
            const vb = this.viewBox;
            return [vb.x + (clientX - box.left) * box.scaleX, vb.y + (clientY - box.top) * box.scaleY];
        },

        // -- wheel-zoom-to-cursor ------------------------------------------

        onWheel(evt) {
            evt.preventDefault();
            const [cursorX, cursorY] = this.clientToViewBox(evt.clientX, evt.clientY);
            const vb = this.viewBox;
            const factor = Math.exp(evt.deltaY * 0.0015);
            const minW = this._fitW * 0.05;
            const maxW = this._fitW * 20;
            const newW = clamp(vb.w * factor, minW, maxW);
            const newH = newW * (vb.h / vb.w);
            const relX = (cursorX - vb.x) / vb.w;
            const relY = (cursorY - vb.y) / vb.h;
            this.viewBox = {
                x: cursorX - relX * newW,
                y: cursorY - relY * newH,
                w: newW,
                h: newH,
            };
        },

        // -- pan on empty background -----------------------------------

        startPan(evt) {
            this._panState = {
                startClientX: evt.clientX,
                startClientY: evt.clientY,
                startX: this.viewBox.x,
                startY: this.viewBox.y,
            };
        },

        onPanMove(evt) {
            if (!this._panState) return;
            const box = this.contentBox();
            const dxClient = evt.clientX - this._panState.startClientX;
            const dyClient = evt.clientY - this._panState.startClientY;
            this.viewBox = {
                ...this.viewBox,
                x: this._panState.startX - dxClient * box.scaleX,
                y: this._panState.startY - dyClient * box.scaleY,
            };
        },

        endPan() {
            this._panState = null;
        },

        isPanning() {
            return this._panState !== null;
        },

        // -- fit ------------------------------------------------------------

        // Fit the viewBox to a tight bounding box (minX, minY, width,
        // height) plus modest padding -- used both for the correct initial
        // fit and the "Fit" toolbar button.
        fitTo(minX, minY, w, h, padRatio) {
            padRatio = padRatio === undefined ? 0.1 : padRatio;
            if (!w || !h || !isFinite(w) || !isFinite(h)) {
                minX = -1;
                minY = -1;
                w = 12;
                h = 12;
            }
            const padX = w * padRatio + 0.75;
            const padY = h * padRatio + 0.75;
            this.viewBox = { x: minX - padX, y: minY - padY, w: w + padX * 2, h: h + padY * 2 };
            this._fitW = this.viewBox.w;
            this._fitH = this.viewBox.h;
        },
    };
}

window.EditorViewport = { mixin };
