/*
 * QR finder-pattern COUNTER -- a "how many codes are in frame?" probe, not a
 * decoder.
 *
 * Why count instead of decode: door staff on iOS Safari have no
 * BarcodeDetector, so scanner.js falls back to vendored jsQR (static/js/jsQR.js)
 * which decodes at most ONE code per call. That's fine for admitting a single
 * ticket, but it CAN'T tell us when several codes are in view at once -- and we
 * must refuse those frames (a monitor or fanned-out stack showing many codes is
 * ambiguous; we don't know which ticket the staffer means). Re-decoding to find
 * a second code is unreliable: the extra codes are usually small, blurry, tilted,
 * or moire-striped off a screen, and simply won't decode.
 *
 * The trick: every QR symbol carries THREE finder patterns -- the big
 * concentric squares in three corners, which read black:white:black:white:black
 * in a 1:1:3:1:1 module ratio along ANY line through their center. Those squares
 * survive degradation that defeats full decoding, so counting them detects codes
 * that are too far gone to read. 3 confirmed centers == one code in view;
 * >= 4 == more than one code, and the caller should refuse to scan.
 *
 * This is a direct, self-contained port of the classic ZXing
 * FinderPatternFinder idea (adaptive binarize -> per-row run-length 1:1:3:1:1
 * scan -> vertical/horizontal crosscheck -> cluster centers). No DOM, no canvas,
 * no dependencies -- it takes raw ImageData bytes and returns a count, so it's
 * cheap enough to run ~8x/second on a 480px camera frame on a mid-range phone.
 *
 * Public API (a test harness is built against this exact signature):
 *   countQrFinderPatterns(data, width, height) -> integer
 */

(function () {
    "use strict";

    // ---- Tuning constants (integrator: adjust these) ------------------------
    // maxVariance rule from ZXing: a run must be within moduleSize/VARIANCE_DIV
    // of its expected size. Larger divisor == stricter. 2.0 is ZXing's value
    // (moduleSize/2); the prompt suggested 1.5 (looser, more forgiving of
    // blur/perspective). We keep 2.0 for the edge runs and allow the center run
    // 3x that slack, matching ZXing's foundPatternCross.
    var VARIANCE_DIV = 2.0;
    // Cluster radius: a new center within max(CLUSTER_MODULES * moduleSize,
    // CLUSTER_MIN_PX) of an existing one merges into it instead of counting
    // twice. The prompt calls for ~3 modules / 6px.
    var CLUSTER_MODULES = 3.0;
    var CLUSTER_MIN_PX = 6.0;
    // A candidate's module size must be within this factor of an existing
    // center's to merge, and horizontal vs. vertical module size within it to
    // pass crosscheck. 2x tolerates perspective without merging unrelated marks.
    var MODULE_RATIO_TOL = 2.0;
    // Rows are scanned every ROW_STEP pixels. 2 halves the work with negligible
    // miss rate because finder squares are several px tall even when tiny.
    var ROW_STEP = 2;
    // Adaptive-binarizer block size is derived from image size but floored here
    // so we always get a local (never global) threshold. See binarize().
    var MIN_BLOCK = 8;

    /*
     * Adaptive binarization into a preallocated Uint8Array (1 = dark, 0 = light).
     *
     * A single global threshold fails on camera frames -- one corner in shadow
     * and half the image reads solid black. So we tile the image into blocks,
     * take each block's own mean luma as its threshold, then smooth by averaging
     * a block with its neighbors (a small blur over the coarse threshold grid).
     * This mirrors the intent of jsQR's binarizer without pulling it in.
     *
     * Returns the Uint8Array bitmap (row-major, width*height).
     */
    function binarize(data, width, height) {
        // Block size scales with the frame but never below MIN_BLOCK, so the
        // threshold is always local. ~1/40th of the short side is a good middle
        // ground: big enough to contain both ink and paper in most blocks.
        var block = Math.max(MIN_BLOCK, Math.floor(Math.min(width, height) / 40));
        var bw = Math.ceil(width / block);
        var bh = Math.ceil(height / block);

        // Per-block mean luma. Integer luma approximation: (R*77 + G*150 + B*29)
        // >> 8 ~= 0.299R + 0.587G + 0.114B, no floating point in the hot loop.
        var means = new Float32Array(bw * bh);
        var bx, by, x, y;
        for (by = 0; by < bh; by++) {
            for (bx = 0; bx < bw; bx++) {
                var x0 = bx * block;
                var y0 = by * block;
                var x1 = Math.min(x0 + block, width);
                var y1 = Math.min(y0 + block, height);
                var sum = 0;
                var count = 0;
                for (y = y0; y < y1; y++) {
                    var rowOff = y * width;
                    for (x = x0; x < x1; x++) {
                        var i = (rowOff + x) << 2;
                        sum += (data[i] * 77 + data[i + 1] * 150 + data[i + 2] * 29) >> 8;
                        count++;
                    }
                }
                means[by * bw + bx] = count > 0 ? sum / count : 128;
            }
        }

        // Smooth the coarse threshold grid: each block's threshold is the mean
        // of it and its 8 neighbors. This bridges a block that happens to be all
        // paper (whose local mean would otherwise threshold noise into ink) to
        // its inked neighbors, killing speckle at block seams.
        var smooth = new Float32Array(bw * bh);
        for (by = 0; by < bh; by++) {
            for (bx = 0; bx < bw; bx++) {
                var acc = 0;
                var n = 0;
                for (var dy = -1; dy <= 1; dy++) {
                    var ny = by + dy;
                    if (ny < 0 || ny >= bh) continue;
                    for (var dx = -1; dx <= 1; dx++) {
                        var nx = bx + dx;
                        if (nx < 0 || nx >= bw) continue;
                        acc += means[ny * bw + nx];
                        n++;
                    }
                }
                smooth[by * bw + bx] = acc / n;
            }
        }

        // Threshold every pixel against its (smoothed) block mean. A pixel
        // strictly darker than the local mean is ink. We nudge the threshold
        // down by a hair (mean is compared with <) so flat paper stays light.
        var bitmap = new Uint8Array(width * height);
        for (y = 0; y < height; y++) {
            var brow = ((y / block) | 0) * bw;
            var outRow = y * width;
            for (x = 0; x < width; x++) {
                var i2 = (outRow + x) << 2;
                var luma = (data[i2] * 77 + data[i2 + 1] * 150 + data[i2 + 2] * 29) >> 8;
                var thr = smooth[brow + ((x / block) | 0)];
                bitmap[outRow + x] = luma < thr ? 1 : 0;
            }
        }
        return bitmap;
    }

    /*
     * ZXing's ratio test: do five run lengths (dark,light,dark,light,dark) form
     * the 1:1:3:1:1 finder cross? moduleSize = total/7; each edge run must be
     * within moduleSize/VARIANCE_DIV of moduleSize, the center run within 3x
     * that of 3*moduleSize. Returns moduleSize (>0) on match, or 0 on reject.
     */
    function checkRatio(r0, r1, r2, r3, r4) {
        var total = r0 + r1 + r2 + r3 + r4;
        if (total < 7) return 0; // can't be a 7-module cross narrower than 7px
        var moduleSize = total / 7.0;
        var maxVariance = moduleSize / VARIANCE_DIV;
        if (Math.abs(moduleSize - r0) >= maxVariance) return 0;
        if (Math.abs(moduleSize - r1) >= maxVariance) return 0;
        if (Math.abs(3.0 * moduleSize - r2) >= 3.0 * maxVariance) return 0;
        if (Math.abs(moduleSize - r3) >= maxVariance) return 0;
        if (Math.abs(moduleSize - r4) >= maxVariance) return 0;
        return moduleSize;
    }

    // Read one pixel of the bitmap (1 dark / 0 light); out-of-bounds is light,
    // which cleanly terminates a run walked off the image edge.
    function px(bitmap, width, height, x, y) {
        if (x < 0 || y < 0 || x >= width || y >= height) return 0;
        return bitmap[y * width + x];
    }

    /*
     * Vertical crosscheck at column centerX around row centerY. Walk up and down
     * counting the same dark/light/dark/light/dark run pattern the center dark
     * run straddling centerY. This is the false-positive killer: a horizontal
     * text stroke can fake the 1:1:3:1:1 pattern along a row, but has no matching
     * vertical structure, so it dies here. Returns the refined vertical center Y
     * (float) on success, or NaN on failure.
     */
    function crossCheckVertical(bitmap, width, height, centerX, centerY, maxCount, originalTotal) {
        var y = centerY;
        var r0 = 0, r1 = 0, r2 = 0, r3 = 0, r4 = 0;

        // Center dark run: extend up while dark.
        while (y >= 0 && px(bitmap, width, height, centerX, y) === 1) { r2++; y--; }
        if (y < 0) return NaN;
        // Light run above.
        while (y >= 0 && px(bitmap, width, height, centerX, y) === 0 && r1 <= maxCount) { r1++; y--; }
        if (y < 0 || r1 > maxCount) return NaN;
        // Dark run above that.
        while (y >= 0 && px(bitmap, width, height, centerX, y) === 1 && r0 <= maxCount) { r0++; y--; }
        if (r0 > maxCount) return NaN;

        // Now downward from just below center.
        y = centerY + 1;
        while (y < height && px(bitmap, width, height, centerX, y) === 1) { r2++; y++; }
        if (y >= height) return NaN;
        while (y < height && px(bitmap, width, height, centerX, y) === 0 && r3 <= maxCount) { r3++; y++; }
        if (y >= height || r3 > maxCount) return NaN;
        while (y < height && px(bitmap, width, height, centerX, y) === 1 && r4 <= maxCount) { r4++; y++; }
        if (r4 > maxCount) return NaN;

        // The vertical extent must be in the same ballpark as the horizontal one
        // (ZXing: reject if it differs from the row total by more than the total
        // itself). Guards against a run that walked into an unrelated blob.
        var total = r0 + r1 + r2 + r3 + r4;
        if (5 * Math.abs(total - originalTotal) >= 2 * originalTotal) return NaN;

        var moduleSize = checkRatio(r0, r1, r2, r3, r4);
        if (moduleSize === 0) return NaN;
        // Refined center: bottom of center run minus half its length.
        return (y - r4 - r3) - r2 / 2.0;
    }

    /*
     * Diagonal crosscheck through (centerX, centerY): walk up-left and
     * down-right counting the same 5-run pattern. A true finder square is
     * concentric, so the 1:1:3:1:1 cross holds along the diagonal too (the
     * runs are sqrt(2) longer, but the RATIO is scale-free). Random blobs in a
     * QR's data region -- the main phantom source when counting patterns on a
     * single code -- frequently pass the two axis checks yet die here.
     * Returns true on match.
     */
    function crossCheckDiagonal(bitmap, width, height, centerX, centerY, maxCount) {
        var r0 = 0, r1 = 0, r2 = 0, r3 = 0, r4 = 0;
        var i;
        // Up-left from center while dark.
        i = 0;
        while (px(bitmap, width, height, centerX - i, centerY - i) === 1) { r2++; i++; }
        if (r2 === 0) return false;
        while (px(bitmap, width, height, centerX - i, centerY - i) === 0 && r1 <= maxCount) { r1++; i++; }
        if (r1 === 0 || r1 > maxCount) return false;
        while (px(bitmap, width, height, centerX - i, centerY - i) === 1 && r0 <= maxCount) { r0++; i++; }
        if (r0 === 0 || r0 > maxCount) return false;
        // Down-right from just past center.
        i = 1;
        while (px(bitmap, width, height, centerX + i, centerY + i) === 1) { r2++; i++; }
        while (px(bitmap, width, height, centerX + i, centerY + i) === 0 && r3 <= maxCount) { r3++; i++; }
        if (r3 === 0 || r3 > maxCount) return false;
        while (px(bitmap, width, height, centerX + i, centerY + i) === 1 && r4 <= maxCount) { r4++; i++; }
        if (r4 === 0 || r4 > maxCount) return false;
        return checkRatio(r0, r1, r2, r3, r4) > 0;
    }

    /*
     * Horizontal crosscheck at row centerY around column centerX -- mirror image
     * of crossCheckVertical. Run after the vertical check refined centerY, to
     * re-center X precisely and reject strokes that only line up one way.
     * Returns refined center X (float) or NaN.
     */
    function crossCheckHorizontal(bitmap, width, height, centerX, centerY, maxCount, originalTotal) {
        var x = centerX;
        var r0 = 0, r1 = 0, r2 = 0, r3 = 0, r4 = 0;

        while (x >= 0 && px(bitmap, width, height, x, centerY) === 1) { r2++; x--; }
        if (x < 0) return NaN;
        while (x >= 0 && px(bitmap, width, height, x, centerY) === 0 && r1 <= maxCount) { r1++; x--; }
        if (x < 0 || r1 > maxCount) return NaN;
        while (x >= 0 && px(bitmap, width, height, x, centerY) === 1 && r0 <= maxCount) { r0++; x--; }
        if (r0 > maxCount) return NaN;

        x = centerX + 1;
        while (x < width && px(bitmap, width, height, x, centerY) === 1) { r2++; x++; }
        if (x >= width) return NaN;
        while (x < width && px(bitmap, width, height, x, centerY) === 0 && r3 <= maxCount) { r3++; x++; }
        if (x >= width || r3 > maxCount) return NaN;
        while (x < width && px(bitmap, width, height, x, centerY) === 1 && r4 <= maxCount) { r4++; x++; }
        if (r4 > maxCount) return NaN;

        var total = r0 + r1 + r2 + r3 + r4;
        if (5 * Math.abs(total - originalTotal) >= 2 * originalTotal) return NaN;

        var moduleSize = checkRatio(r0, r1, r2, r3, r4);
        if (moduleSize === 0) return NaN;
        return (x - r4 - r3) - r2 / 2.0;
    }

    /*
     * Merge a confirmed candidate into the running list of centers, or add it as
     * a new distinct center. Two hits merge when they're close in space (within
     * max(CLUSTER_MODULES*moduleSize, CLUSTER_MIN_PX)) AND compatible in scale
     * (module sizes within MODULE_RATIO_TOL). Merging averages the positions and
     * bumps a hit counter, so a finder square crossed by several scan rows counts
     * once, not once per row.
     */
    function addCenter(centers, x, y, moduleSize) {
        for (var i = 0; i < centers.length; i++) {
            var c = centers[i];
            var radius = Math.max(CLUSTER_MODULES * Math.max(moduleSize, c.moduleSize), CLUSTER_MIN_PX);
            var dx = c.x - x;
            var dy = c.y - y;
            if (dx * dx + dy * dy <= radius * radius) {
                var ratio = moduleSize / c.moduleSize;
                if (ratio < 1) ratio = 1 / ratio;
                if (ratio <= MODULE_RATIO_TOL) {
                    // Weighted-ish average: simple mean is enough for a count.
                    var n = c.hits + 1;
                    c.x = (c.x * c.hits + x) / n;
                    c.y = (c.y * c.hits + y) / n;
                    c.moduleSize = (c.moduleSize * c.hits + moduleSize) / n;
                    c.hits = n;
                    return;
                }
            }
        }
        centers.push({ x: x, y: y, moduleSize: moduleSize, hits: 1 });
    }

    /*
     * Find confirmed, deduplicated finder-pattern centers in an RGBA frame.
     *
     * data:   Uint8ClampedArray of RGBA pixels (length width*height*4), exactly
     *         as returned by CanvasRenderingContext2D.getImageData().data.
     * width,
     * height: frame dimensions in pixels.
     * Returns an array of { x, y, moduleSize, hits } in frame pixel coords.
     * Only centers confirmed on >= MIN_HITS scan rows are returned: a real
     * finder square several pixels tall is crossed by multiple scan rows that
     * all converge on the same center, while one-row noise never repeats.
     */
    var MIN_HITS = 2;

    function findQrFinderPatterns(data, width, height) {
        if (!data || width < 7 || height < 7) return [];

        var bitmap = binarize(data, width, height);
        var centers = [];

        // Rolling window of the last five runs on the current row. We only test
        // when the window is [dark, light, dark, light, dark], i.e. the newest
        // completed run was light and closed a dark run (state machine below).
        var runs = [0, 0, 0, 0, 0];

        for (var y = 0; y < height; y += ROW_STEP) {
            // Reset run tracking at the start of each row.
            runs[0] = 0; runs[1] = 0; runs[2] = 0; runs[3] = 0; runs[4] = 0;
            var runCount = 0; // how many runs recorded so far this row (caps at 5, shifts)
            var rowOff = y * width;
            var currentIsDark = false; // color of the run we're currently accumulating
            var runLen = 0;

            // Prime with the first pixel's color.
            currentIsDark = bitmap[rowOff] === 1;

            for (var x = 0; x <= width; x++) {
                // Sentinel light pixel one past the row edge closes any trailing
                // run so a finder touching the right edge still gets tested.
                var isDark = x < width ? bitmap[rowOff + x] === 1 : false;

                if (x < width && isDark === currentIsDark) {
                    runLen++;
                    continue;
                }

                // Color changed (or row ended) -- push the just-finished run.
                shiftRun(runs, runLen);
                if (runCount < 5) runCount++;

                // Runs strictly alternate color (we shift on every color
                // change), so the window [r0,r1,r2,r3,r4] reads dark,light,dark,
                // light,dark exactly when the newest run (runs[4] -- the one we
                // just closed, whose color is currentIsDark) is DARK. That's the
                // only arrangement that can be a 1:1:3:1:1 finder cross, so test
                // only then, once the window is full.
                if (runCount === 5 && currentIsDark) {
                    tryRowCandidate(bitmap, width, height, runs, x, y, centers);
                }

                // Start the next run with the new color.
                currentIsDark = isDark;
                runLen = 1;
            }
        }

        var strong = [];
        for (var i = 0; i < centers.length; i++) {
            if (centers[i].hits >= MIN_HITS) strong.push(centers[i]);
        }
        return strong;
    }

    /*
     * Count distinct QR finder-pattern centers in an RGBA frame.
     * 3 => one QR in view; >= 4 => more than one code (caller should refuse).
     */
    function countQrFinderPatterns(data, width, height) {
        return findQrFinderPatterns(data, width, height).length;
    }

    // Shift the 5-run window left by one and append the newest run length.
    function shiftRun(runs, len) {
        runs[0] = runs[1];
        runs[1] = runs[2];
        runs[2] = runs[3];
        runs[3] = runs[4];
        runs[4] = len;
    }

    /*
     * Given a row window [r0..r4] ending at column x on row y that just passed
     * (or is about to be tested for) the 1:1:3:1:1 shape, confirm it with
     * crosschecks and, if good, cluster it into `centers`.
     */
    function tryRowCandidate(bitmap, width, height, runs, x, y, centers) {
        var r0 = runs[0], r1 = runs[1], r2 = runs[2], r3 = runs[3], r4 = runs[4];
        var moduleSize = checkRatio(r0, r1, r2, r3, r4);
        if (moduleSize === 0) return;

        // Horizontal center of the cross: x is one past the end of runs[4], so
        // back up through r4 and half of the center run r2.
        var total = r0 + r1 + r2 + r3 + r4;
        var centerX = (x - r4 - r3) - r2 / 2.0;

        // maxCount bounds how far a crosscheck run may extend before we call it
        // noise -- a couple of center-run widths, following ZXing.
        var maxCount = Math.ceil(r2);

        // Vertical crosscheck refines Y (and rejects horizontal-only strokes).
        var centerY = crossCheckVertical(bitmap, width, height, Math.round(centerX), y, maxCount, total);
        if (isNaN(centerY)) return;

        // Horizontal crosscheck at the refined Y re-centers X and rejects
        // vertical-only strokes.
        var refinedX = crossCheckHorizontal(bitmap, width, height, Math.round(centerX), Math.round(centerY), maxCount, total);
        if (isNaN(refinedX)) return;

        // Diagonal crosscheck last -- the cheapest-to-fake direction for a
        // phantom is already gone, and true concentric squares pass this too.
        if (!crossCheckDiagonal(bitmap, width, height, Math.round(refinedX), Math.round(centerY), maxCount)) return;

        addCenter(centers, refinedX, centerY, moduleSize);
    }

    // ---- Exports (both browser global and CommonJS, per the API contract) ----
    // countQrFinderPatterns keeps its original signature; findQrFinderPatterns
    // exposes the centers so scanner.js can ignore patterns that belong to a
    // code jsQR already decoded and located.
    if (typeof window !== "undefined") {
        window.countQrFinderPatterns = countQrFinderPatterns;
        window.findQrFinderPatterns = findQrFinderPatterns;
    }
    if (typeof module !== "undefined" && module.exports) {
        module.exports = countQrFinderPatterns;
        module.exports.count = countQrFinderPatterns;
        module.exports.find = findQrFinderPatterns;
    }
})();
